import { render, screen } from "@testing-library/react";
import OrderSummary, { calculateOrderSummary, OrderItem, OrderSummaryProps } from "../order-summary";

describe("OrderSummary", () => {
    const sampleItems: OrderItem[] = [
        { item: "Caramel Craze Latte", size: "medium", quantity: 2, price: 4.99, display: "Medium Caramel Craze Latte" },
        { item: "Glazed Donut", size: "standard", quantity: 1, price: 1.49, display: "Glazed Donut" }
    ];

    it("renders Dunkin items with the correct totals", () => {
        const summary = calculateOrderSummary(sampleItems);
        render(<OrderSummary order={summary} />);

        expect(screen.getByText("Your Dunkin Order")).toBeInTheDocument();
        expect(screen.getByText(/Medium Caramel Craze Latte/)).toBeInTheDocument();
        expect(screen.getByText(/Glazed Donut/)).toBeInTheDocument();
        expect(screen.getByText(`$${summary.total.toFixed(2)}`)).toBeInTheDocument();
        expect(screen.getByText(`$${summary.finalTotal.toFixed(2)}`)).toBeInTheDocument();
    });

    it("shows the empty-state helper when no items are present", () => {
        const emptySummary: OrderSummaryProps = { items: [], total: 0, tax: 0, finalTotal: 0 };
        render(<OrderSummary order={emptySummary} />);

        expect(screen.getByText(/Add a donut, latte, or sandwich/i)).toBeInTheDocument();
    });
});
