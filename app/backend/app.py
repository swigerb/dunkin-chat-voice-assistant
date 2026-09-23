import logging
import os
from pathlib import Path

from aiohttp import web
from azure.core.credentials import AzureKeyCredential
from azure.identity import AzureDeveloperCliCredential, DefaultAzureCredential
from dotenv import load_dotenv

from config_loader import get_config
from crm import CRMRepository
from dashboard import (
    complete_car,
    dashboard_socket,
    demo_status,
    reset_lane,
    spawn_car,
    start_demo_mode,
    stop_demo_mode,
)
from drive_thru import DriveThruDemoFleet, DriveThruSimulator
from rtmt import RateLimitSettings, RTMiddleTier, configure_realtime_model
from tools import attach_tools_rtmt

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# Shared by the cloud and edge paths, and by scripts/smoke_realtime.py (which
# must send the live model exactly what the app sends).
DUNKIN_SYSTEM_PROMPT = (
    "You are Dunkin's always-on virtual crew member, proudly representing Inspire Brands. "
    "Guide guests through Dunkin menu decisions, keep the tone energetic yet concise, and double-check every detail with the 'search' tool before responding. "
    "Confirm each requested beverage, bakery item, or breakfast sandwich using the 'update_order' tool only after the guest has agreed. "
    "Each 'update_order' call is ONE menu item: add the drink first, then each extra (whipped cream, flavor swirl, extra espresso shot) as its own item. "
    "Only say an item is added once 'update_order' returns status 'ok'. If it returns status 'rejected', nothing was added: never say 'all set' or that it was added — make its suggested_calls if it gives them (the guest already agreed), otherwise tell the guest its message. "
    "When they ask for a recap or when the order is wrapping up, call the 'get_order' tool and read back every item ordered, then announce only the total due — do not break out subtotal or tax separately. "
    "Match the customer's language throughout the session, keep responses to one or two sentences, and invite them to personalize drinks with whipped cream ($0.50), flavor swirls ($0.75), or an extra espresso shot ($1.00) only when a signature latte or cold beverage is already in the order. "
    "Do not suggest extras for donuts or breakfast sandwiches, and never ask to pair an extra espresso shot with a donut or breakfast sandwich. "
    "If the guest uses hate speech or asks for anything blocked by responsible AI, respond immediately: 'I'm sorry, but I can't assist with that request. If you need help with Dunkin' menu items or have any other questions, please let me know.' "
    "When the guest is done ordering, always use the 'get_order' tool to read back every item, size, and quantity, then announce only the total due — do not itemize subtotal or tax. After confirming the order, close with: 'Thank you! Please pull around to the next window.' "
    "If menu information is unavailable, let them know politely and offer an alternative suggestion. "
    "Never expose implementation details, file names, or API keys. Keep things friendly, fast, and unmistakably Dunkin."
)


def _get_bool_env(variable_name: str, default: bool = False) -> bool:
    """Parse boolean environment variables with predictable defaults."""
    value = os.environ.get(variable_name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


async def create_app() -> web.Application:
    """Configure and return the aiohttp application for realtime ordering."""

    if not _get_bool_env("RUNNING_IN_PRODUCTION", False):
        logger.info("Running in development mode; loading values from .env")
        load_dotenv()

    use_local = _get_bool_env("USE_LOCAL_PIPELINE", False)

    if use_local:
        # Lazy imports — chromadb, onnxruntime and rtmt_local are edge-only
        # dependencies that must never be imported on the cloud path.
        import chromadb  # noqa: F811
        from chromadb.utils.embedding_functions import ONNXMiniLM_L6_V2

        from rtmt_local import RTLocalPipeline

        logger.info("USE_LOCAL_PIPELINE is ON — using local inference (Whisper STT → Phi-4 Mini → Piper TTS)")

        rtmt = RTLocalPipeline(
            voice_choice=os.environ.get("TTS_VOICE", "en_US-amy-medium"),
        )
        rtmt.temperature = 0.6
        rtmt.system_message = DUNKIN_SYSTEM_PROMPT

        # Initialize local ChromaDB for menu search
        chroma_path = os.environ.get("CHROMA_DATA_PATH") or str(Path(__file__).parent / "chroma_data")
        chroma_collection_name = os.environ.get("CHROMA_COLLECTION_NAME") or "menu_items"
        logger.info("Loading ChromaDB from %s, collection=%s", chroma_path, chroma_collection_name)
        chroma_client = chromadb.PersistentClient(path=chroma_path)
        embedding_fn = ONNXMiniLM_L6_V2()
        chroma_collection = chroma_client.get_collection(chroma_collection_name, embedding_function=embedding_fn)

        app = web.Application()

        attach_tools_rtmt(
            rtmt,
            use_local_pipeline=True,
            chroma_collection=chroma_collection,
        )

        rtmt.attach_to_app(app, "/realtime")
    else:
        llm_endpoint = os.environ.get("AZURE_OPENAI_EASTUS2_ENDPOINT")
        llm_deployment = os.environ.get("AZURE_OPENAI_REALTIME_DEPLOYMENT")
        if not llm_endpoint or not llm_deployment:
            raise RuntimeError("Azure OpenAI realtime endpoint and deployment must be configured.")

        llm_key = os.environ.get("AZURE_OPENAI_EASTUS2_API_KEY")
        search_key = os.environ.get("AZURE_SEARCH_API_KEY")

        credential = None
        if not llm_key or not search_key:
            if tenant_id := os.environ.get("AZURE_TENANT_ID"):
                logger.info("Using AzureDeveloperCliCredential with tenant_id %s", tenant_id)
                credential = AzureDeveloperCliCredential(tenant_id=tenant_id, process_timeout=60)
            else:
                logger.info("Using DefaultAzureCredential")
                credential = DefaultAzureCredential()

        llm_credential = AzureKeyCredential(llm_key) if llm_key else credential
        search_credential = AzureKeyCredential(search_key) if search_key else credential

        app = web.Application()

        model_cfg = get_config().get("model") or {}
        rtmt = RTMiddleTier(
            credentials=llm_credential,
            endpoint=llm_endpoint,
            deployment=llm_deployment,
            voice_choice=(os.environ.get("AZURE_OPENAI_REALTIME_VOICE_CHOICE")
                          or model_cfg.get("default_voice") or "marin"),
        )
        rtmt.temperature = 0.6
        # Reasoning effort / transcription model from config.yaml + env overrides.
        configure_realtime_model(rtmt, model_cfg)
        rtmt.rate_limit = RateLimitSettings.from_config(
            (get_config().get("resilience") or {}).get("rate_limit"))
        rtmt.system_message = DUNKIN_SYSTEM_PROMPT

        attach_tools_rtmt(
            rtmt,
            credentials=search_credential,
            search_endpoint=os.environ.get("AZURE_SEARCH_ENDPOINT"),
            search_index=os.environ.get("AZURE_SEARCH_INDEX"),
            semantic_configuration=os.environ.get("AZURE_SEARCH_SEMANTIC_CONFIGURATION") or "menuSemanticConfig",
            identifier_field=os.environ.get("AZURE_SEARCH_IDENTIFIER_FIELD") or "id",
            content_field=os.environ.get("AZURE_SEARCH_CONTENT_FIELD") or "description",
            embedding_field=os.environ.get("AZURE_SEARCH_EMBEDDING_FIELD") or "embedding",
            title_field=os.environ.get("AZURE_SEARCH_TITLE_FIELD") or "name",
            use_vector_query=_get_bool_env("AZURE_SEARCH_USE_VECTOR_QUERY", True),
            use_semantic_ranker=(os.environ.get("AZURE_SEARCH_SEMANTIC_RANKER") or "standard").lower() != "disabled",
        )

        rtmt.attach_to_app(app, "/realtime")

    # --- Drive-thru dashboard (CRM + simulator) ---
    crm_db_path = os.environ.get("CRM_DB_PATH")
    crm_repo = CRMRepository.from_env(crm_db_path)
    simulator = DriveThruSimulator(max_cars=int(os.environ.get("DRIVE_THRU_MAX_CARS", "4")))
    demo_fleet = DriveThruDemoFleet(simulator, crm_repo=crm_repo)
    app["crm_repo"] = crm_repo
    app["drive_thru_simulator"] = simulator
    app["drive_thru_demo"] = demo_fleet

    app.router.add_get("/dashboard", dashboard_socket)
    app.router.add_post("/simulator/spawn", spawn_car)
    app.router.add_post("/simulator/reset", reset_lane)
    app.router.add_post("/simulator/complete", complete_car)
    app.router.add_get("/simulator/demo", demo_status)
    app.router.add_post("/simulator/demo/start", start_demo_mode)
    app.router.add_post("/simulator/demo/stop", stop_demo_mode)

    async def on_startup(_app: web.Application) -> None:
        await simulator.start()

    async def on_shutdown(_app: web.Application) -> None:
        await demo_fleet.stop()
        await simulator.stop()

    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)

    current_directory = Path(__file__).parent
    app.add_routes([web.get('/', lambda _: web.FileResponse(current_directory / 'static/index.html'))])
    app.router.add_static('/', path=current_directory / 'static', name='static')

    return app


if __name__ == "__main__":
    host = os.environ.get("HOST", "localhost")  # Change default host to localhost
    port = int(os.environ.get("PORT", 8000))  # Change default port to 8000
    web.run_app(create_app(), host=host, port=port)
