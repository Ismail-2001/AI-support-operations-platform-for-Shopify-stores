import { render, screen, fireEvent } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import { SearchInput } from "../SearchInput";

describe("SearchInput", () => {
  it("renders with placeholder", () => {
    render(<SearchInput value="" onChange={() => {}} placeholder="Search tickets" />);
    expect(screen.getByPlaceholderText("Search tickets")).toBeInTheDocument();
  });

  it("renders default placeholder", () => {
    render(<SearchInput value="" onChange={() => {}} />);
    expect(screen.getByPlaceholderText("Search…")).toBeInTheDocument();
  });

  it("displays current value", () => {
    render(<SearchInput value="test query" onChange={() => {}} />);
    expect(screen.getByDisplayValue("test query")).toBeInTheDocument();
  });

  it("calls onChange when typing", () => {
    const onChange = vi.fn();
    render(<SearchInput value="" onChange={onChange} />);
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "new" } });
    expect(onChange).toHaveBeenCalledWith("new");
  });

  it("shows clear button when value is non-empty", () => {
    render(<SearchInput value="something" onChange={() => {}} />);
    expect(screen.getByRole("button")).toBeInTheDocument();
  });

  it("hides clear button when value is empty", () => {
    const { container } = render(<SearchInput value="" onChange={() => {}} />);
    expect(container.querySelector("button")).not.toBeInTheDocument();
  });

  it("clears value when clear button clicked", () => {
    const onChange = vi.fn();
    render(<SearchInput value="something" onChange={onChange} />);
    fireEvent.click(screen.getByRole("button"));
    expect(onChange).toHaveBeenCalledWith("");
  });
});
