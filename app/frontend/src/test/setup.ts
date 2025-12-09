import "@testing-library/jest-dom";
import { vi } from "vitest";

vi.mock("react-i18next", () => ({
    useTranslation: () => ({
        t: (key: string) => key,
        i18n: { language: "en", changeLanguage: () => Promise.resolve() }
    }),
    initReactI18next: {
        type: "3rdParty",
        init: () => undefined
    }
}));
