import {render, screen} from "@testing-library/react";
import {expect, test} from "vitest";
import {isWireNumber, materialize, parseContractJson, type WireNumber} from "../api/contract-numeric";
import {useSort} from "./sort-header";

function integer(token: string): WireNumber {
  const value = materialize(parseContractJson(token));
  if (!isWireNumber(value)) throw new Error("Fixture is not a wire integer");
  return value;
}

function DiskRows() {
  const rows = [
    {name: "Higher", disk: integer("9007199254740993")},
    {name: "Unknown", disk: null},
    {name: "Lower", disk: integer("9007199254740992")},
  ];
  const {sorted} = useSort(rows, {key: "disk", descending: false}, {disk: row => row.disk});
  return <ol>{sorted.map(row => <li key={row.name}>{row.name}</li>)}</ol>;
}

test("sorts adjacent wide disk measurements exactly and keeps unknown last", () => {
  render(<DiskRows/>);
  expect(screen.getAllByRole("listitem").map(item => item.textContent)).toEqual(["Lower", "Higher", "Unknown"]);
});
