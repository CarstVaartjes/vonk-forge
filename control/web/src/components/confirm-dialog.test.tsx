import {render, screen} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {useState} from "react";
import {ConfirmDialog} from "./confirm-dialog";

function Harness({onConfirm = () => undefined, typeName}: {onConfirm?(): void; typeName?: string}) {
  const [open, setOpen] = useState(false);
  return <>
    <button type="button" onClick={() => setOpen(true)}>Open</button>
    {open && <ConfirmDialog title="Sure?" consequence="It happens." confirmLabel="Do it" typeName={typeName} command="vonkctl x --yes" onConfirm={onConfirm} onCancel={() => setOpen(false)}/>}
  </>;
}

test("the dialog is modal, traps Tab inside, closes on Escape and returns focus to its trigger", async () => {
  const user = userEvent.setup();
  render(<Harness/>);
  const trigger = screen.getByRole("button", {name: "Open"});
  await user.click(trigger);
  const dialog = screen.getByRole("alertdialog");
  expect(dialog).toHaveAttribute("aria-modal", "true");
  for (let step = 0; step < 8; step++) {
    await user.tab();
    expect(dialog).toContainElement(document.activeElement as HTMLElement);
  }
  await user.tab({shift: true});
  expect(dialog).toContainElement(document.activeElement as HTMLElement);
  await user.keyboard("{Escape}");
  expect(screen.queryByRole("alertdialog")).toBeNull();
  expect(trigger).toHaveFocus();
});

test("irreversible actions wait for the name to be typed and show the CLI equivalent", async () => {
  const user = userEvent.setup();
  const onConfirm = vi.fn();
  render(<Harness typeName="laptop" onConfirm={onConfirm}/>);
  await user.click(screen.getByRole("button", {name: "Open"}));
  expect(screen.getByText("vonkctl x --yes")).toBeVisible();
  expect(screen.getByRole("button", {name: "Do it"})).toBeDisabled();
  await user.type(screen.getByRole("textbox"), "laptop");
  await user.click(screen.getByRole("button", {name: "Do it"}));
  expect(onConfirm).toHaveBeenCalledOnce();
});

test("the rest of the page is inert while the dialog is open and live again after", async () => {
  const user = userEvent.setup();
  const {container} = render(<Harness/>);
  await user.click(screen.getByRole("button", {name: "Open"}));
  expect(container.inert).toBe(true);
  await user.keyboard("{Escape}");
  expect(container.inert).toBe(false);
});
