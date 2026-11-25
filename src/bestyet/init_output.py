"""Init script output: grouped plan text, Done and failure text, next steps and printing.

No Grist calls and no questions: it turns plans, steps and errors into lines for a person.
"""

import argparse
from collections.abc import Sequence

from bestyet import schema
from bestyet.config import Config
from bestyet.grist import GristError
from bestyet.init_errors import InitError, explain
from bestyet.init_plan import Plan, implement_event_names
from bestyet.init_steps import (
    DEFAULT_TABLE,
    AddColumns,
    CreateSummaryPage,
    CreateTable,
    LoadDemoData,
    SeedEvents,
    SeedImplements,
    SetVisibleCols,
    Step,
)


class PartialFailure(InitError):
    """The run stopped part-way: what was done, what failed and what was not done."""

    def __init__(
        self,
        done: Sequence[str],
        applied: Sequence[Step],
        pending: Sequence[Step],
        error: Exception,
        attention: Sequence[str] = (),
    ):
        super().__init__(str(error))
        self.done, self.applied, self.pending = list(done), list(applied), list(pending)
        self.error = error
        self.attention = list(attention)

    @property
    def rerun_safe(self) -> bool:
        """A plain re-run (which only adds) fixes it: the failure was a Grist call."""
        return isinstance(self.error, GristError)

    def _incomplete(self) -> list[tuple[str, list[str]]]:
        """What did not complete, in order, the failed step marked."""
        merged: dict[str, list[str]] = {}
        for heading, lines in grouped(self.pending[:1], hidden=True):
            merged[heading] = [f"{line} (failed; may be partly applied)" for line in lines]
        for heading, lines in grouped(self.pending[1:], hidden=True):
            merged.setdefault(heading, []).extend(lines)
        return list(merged.items())

    def report(self, config: Config, steps: Sequence[str]) -> str:
        reason = explain(self.error, config, "document") if self.rerun_safe else str(self.error)
        done = ([("Document", self.done)] if self.done else []) + grouped(
            self.applied, past=True, hidden=True
        )
        return error_report(
            reason,
            done=done,
            incomplete=self._incomplete(),
            steps=[*steps, *self.attention],
        )


def error_report(
    reason: str,
    done: Sequence[tuple[str, Sequence[str]]] = (),
    incomplete: Sequence[tuple[str, Sequence[str]]] = (),
    steps: Sequence[str] = (),
) -> str:
    """Labelled blocks, blank line between, empty blocks left out."""
    blocks = [["--- Error ---", reason, "The run stopped part-way."]]
    for title, groups in (
        ("What was done", done),
        ("What did not complete", incomplete),
    ):
        if groups:
            blocks.append([f"--- {title} ---", *group_lines(groups)])
    if steps:
        blocks.append(["--- Next steps ---", *(f"  {i}. {t}" for i, t in enumerate(steps, 1))])
    return "\n\n".join("\n".join(b) for b in blocks)


# Plan and summary text: what the user cares about, grouped, not one line per internal step.

GROUPS = ("Tables", "Calculated columns", "Personal bests", "Display", "Pre-loaded data", "Other")


def grouped(
    steps: Sequence[Step], past: bool = False, hidden: bool = False
) -> list[tuple[str, list[str]]]:
    """Group steps by outcome, in plain words (`past`: what was done). The behind-the-scenes
    steps (calculated columns, names in Ref columns) show only when `hidden`: a failure report
    needs them, the plan does not."""
    out: dict[str, list[str]] = {g: [] for g in GROUPS}
    behind_the_scenes = False
    page_made = any(isinstance(s, CreateSummaryPage) for s in steps)
    new = [s.table.id for s in steps if isinstance(s, CreateTable)]
    if new:
        out["Tables"].append(f"{'Created' if past else 'Create'} {', '.join(new)}")
    example: list[str] = []
    for s in steps:
        if isinstance(s, AddColumns) and s.table == schema.SUMMARY.id:
            if not page_made:
                verb = "Added" if past else "Add"
                noun = "column" if len(s.columns) == 1 else "columns"
                out["Personal bests"].append(
                    f"{verb} the missing {noun} to the summary table: {s._names()}"
                )
        elif isinstance(s, AddColumns) and s.columns[0].formula is None:
            verb = "Added columns to" if past else "Add missing columns to"
            out["Tables"].append(f"{verb} {s.table}: {s._names()}")
        elif isinstance(s, AddColumns) and not hidden:
            behind_the_scenes = True
        elif isinstance(s, AddColumns):
            worked = [c.id for c in s.columns if c.is_formula]
            filled = [c.id for c in s.columns if not c.is_formula]
            parts = []
            if worked:
                parts.append(f"{', '.join(worked)} (worked out by Grist)")
            if filled:
                parts.append(f"{', '.join(filled)} (filled in automatically)")
            out["Calculated columns"].append(f"{s.table}: {', '.join(parts)}")
        elif isinstance(s, CreateSummaryPage):
            verb = "Created" if past else "Create"
            out["Personal bests"].append(
                f"{verb} a {s.summary.page!r} page showing each athlete's best per event"
            )
        elif isinstance(s, SetVisibleCols) and not hidden:
            behind_the_scenes = True
        elif isinstance(s, SetVisibleCols) and not out["Display"]:
            out["Display"].append(
                "Set Ref columns to show names instead of ids"
                if past
                else "Show names instead of ids in the Ref columns"
            )
        elif isinstance(s, SeedEvents):
            example.append(_seed_events_text(s, past))
        elif isinstance(s, SeedImplements):
            example.append(_seed_text("sample implement", s.add, s.have, past))
        elif isinstance(s, LoadDemoData):
            example.append("Added demo data" if past else f"Add demo data: {s._counts()}")
    out["Pre-loaded data"].extend(example)
    if behind_the_scenes and not any(out.values()):
        out["Other"].append("Update the existing tables (calculated columns, names in Ref columns)")
    return [(g, lines) for g, lines in out.items() if lines]


def _seed_text(noun: str, add: int, have: int, past: bool) -> str:
    """ "Add the 18 default events", or "Add 5 default events (13 already there)"."""
    verb = "Added" if past else "Add"
    plural = noun if add == 1 else f"{noun}s"
    if have == 0 and add > 1:
        return f"{verb} the {add} {plural}"
    note = "" if past or have == 0 else f" ({have} already there)"
    return f"{verb} {add} {plural}{note}"


def _seed_events_text(step: SeedEvents, past: bool) -> str:
    """The full list as `_seed_text`, otherwise the few events the other seeds need: "Add 2
    events required for the sample implements (Discus and Shot Put)"."""
    add = len(step.add_names)
    if step.names is None or len(step.names) == len(schema.EXAMPLE_EVENTS):
        return _seed_text("default event", add, step.have, past)
    first = implement_event_names()  # the Implements events first, as in the guided note
    names = sorted(step.add_names, key=lambda n: first.index(n) if n in first else len(first))
    verb = "Added" if past else "Add"
    noun = "event" if add == 1 else "events"
    reason = "the demo data and sample implements" if step.for_demo else "the sample implements"
    note = "" if past or step.have == 0 else f"; {step.have} already there"
    return f"{verb} {add} {noun} required for {reason} ({_and(names)}{note})"


def _the_tables(names: Sequence[str]) -> str:
    """ "the table A" or "the tables A, B and C"."""
    unique = list(dict.fromkeys(names))
    return f"the table {unique[0]}" if len(unique) == 1 else f"the tables {_and(unique)}"


def _and(items: Sequence[str]) -> str:
    return items[0] if len(items) == 1 else f"{', '.join(items[:-1])} and {items[-1]}"


def done_message(applied: Sequence[Step], doc: tuple[str, str] | None) -> str:
    """One sentence for a run that went as planned. `doc`: (name, workspace) if created."""
    tables = [s.table.id for s in applied if isinstance(s, CreateTable)]
    pages = [s.summary.page for s in applied if isinstance(s, CreateSummaryPage)]
    columns = [
        s.table
        for s in applied
        if isinstance(s, AddColumns)
        and s.table != schema.SUMMARY.id
        and s.columns[0].formula is None
    ]
    example = any(isinstance(s, (SeedEvents, SeedImplements, LoadDemoData)) for s in applied)
    if doc:
        parts = [f"{len(tables)} table{'' if len(tables) == 1 else 's'}"] if tables else []
        parts += [f"the {p!r} page" for p in pages]
        parts += ["pre-loaded data"] if example else []
        with_ = f" with {_and(parts)}" if parts else ""
        return f"Done. Created {doc[0]!r} in {doc[1]}{with_}."
    parts = [_the_tables(tables)] if tables else []
    parts += [f"missing columns in {_the_tables(columns)}"] if columns else []
    parts += [f"the {p!r} page" for p in pages]
    parts += ["pre-loaded data"] if example else []
    if not parts:
        parts = ["the missing behind-the-scenes parts"]
    return f"Done. Added {_and(parts)}."


def group_lines(groups: Sequence[tuple[str, Sequence[str]]]) -> list[str]:
    lines = []
    for heading, items in groups:
        lines.append(f"  {heading}")
        lines += [f"    - {item}" for item in items]
    return lines


def _section(out, title: str) -> None:
    """A plain-text heading between the parts of a run."""
    print(f"\n--- {title} ---", file=out)


def _print_bullets(heading: str, items: Sequence[str], out) -> None:
    print(heading, file=out)
    for item in items:
        print(f"  - {item}", file=out)


def _print_groups(groups: Sequence[tuple[str, Sequence[str]]], out) -> None:
    for line in group_lines(groups):
        print(line, file=out)


def next_step(allow_existing: bool) -> str:
    if allow_existing:
        return f"remove {DEFAULT_TABLE} in Grist"
    return f"remove {DEFAULT_TABLE} in Grist, or pass --allow-existing-tables on re-runs"


def rerun_hint(doc_id: str) -> str:
    return (
        f"The document was created (id {doc_id}). To continue, re-run without --create and "
        f"with GRIST_DOC_ID={doc_id} (add --allow-existing-tables if {DEFAULT_TABLE} is still "
        "there); it only adds what is missing."
    )


def _fix_step(rerun_safe: bool) -> str:
    return (
        "Fix the cause shown under Error, if you can."
        if rerun_safe
        else "Fix the problem described under Error in Grist."
    )


def _settings_line(doc_id: str, old_doc_id: str) -> str:
    return "{}: {}".format(*settings_step(doc_id, old_doc_id)[:2])


def next_steps_after_create(
    doc_id: str, rerun_safe: bool, old_doc_id: str = "", table1_left: bool = False
) -> list[str]:
    """What to do when a run stopped after the document was created."""
    steps = [
        _fix_step(rerun_safe),
        _settings_line(doc_id, old_doc_id),
        "Then run bestyet-init again (without --create)"
        + ("; it only adds what is missing." if rerun_safe else "."),
    ]
    if table1_left:
        steps.append(
            f"If {DEFAULT_TABLE} is still in the document, add --allow-existing-tables to that run."
        )
    if not rerun_safe:
        steps.append("Do not run --create again: it would make another document.")
    return steps


def not_created_warning() -> str:
    return f"{DEFAULT_TABLE} not removed: the document was not created by this run"


def settings_step(doc_id: str, old_doc_id: str) -> tuple[str, str, str | None]:
    """The settings step: (instruction, the line for the settings, a closing note or None)."""
    assignment = f"GRIST_DOC_ID={doc_id}"
    if old_doc_id:
        update = (
            "Update GRIST_DOC_ID in your settings (environment or .env), which currently "
            "points at a different document"
        )
        note = "Until you do, bestyet-init and the app keep using that other document."
        return update, assignment, note
    return "Add this line to your settings (environment or .env)", assignment, None


EVENTS_HINT = (
    "Add your events by hand in the Events table in Grist (Name, Kind: Time or "
    "Distance, and Category); without events you cannot record results."
)


IMPLEMENTS_HINT = (
    "Add implement weights by hand in the Implements table in Grist (Event, Grade, Division "
    "and Weight); until you do, the Implement column on results stays blank."
)


def print_next_steps(
    out,
    doc_id: str | None,
    old_doc_id: str,
    check: bool = True,
    events_hint: bool = False,
    implements_hint: bool = False,
) -> None:
    """The closing Next steps: settings, events hint, implements hint, then the --check
    pointer; numbered when there are several."""
    if not doc_id and not check and not events_hint and not implements_hint:
        return
    _section(out, "Next steps")
    numbered = bool(doc_id) + events_hint + implements_hint + check > 1
    n = 0
    if doc_id:
        instruction, assignment, note = settings_step(doc_id, old_doc_id)
        n += 1
        print(f"  {n}. {instruction}; this script does not change your settings:", file=out)
        print(f"     {assignment}", file=out)
        if note:
            print(f"     {note}", file=out)
    if events_hint:
        n += 1
        print(f"  {n}. {EVENTS_HINT}" if numbered else f"  - {EVENTS_HINT}", file=out)
    if implements_hint:
        n += 1
        print(f"  {n}. {IMPLEMENTS_HINT}" if numbered else f"  - {IMPLEMENTS_HINT}", file=out)
    if check:
        text = "run bestyet-init --check to confirm the document matches the contract."
        n += 1
        if numbered:
            print(f"  {n}. Then {text}", file=out)
        else:
            print(f"  - {text[0].upper()}{text[1:]}", file=out)


def _new_document_groups(plan: Plan, new_doc: tuple[str, str]) -> list[tuple[str, list[str]]]:
    """A new document's tables and page share one heading; the rest follows as usual."""
    name, label = new_doc
    tables = [s.table.id for s in plan.steps if isinstance(s, CreateTable)]
    pages = [s.summary.page for s in plan.steps if isinstance(s, CreateSummaryPage)]
    lines = [f"Tables: {', '.join(tables)}"] if tables else []
    lines += [f"A {p!r} page showing each athlete's best per event" for p in pages]
    rest = [(h, ls) for h, ls in grouped(plan.steps) if h not in ("Tables", "Personal bests")]
    return [(f"New document {name!r} in {label}", lines), *rest]


def _show_plan(plan: Plan, new_doc: tuple[str, str] | None, args: argparse.Namespace, out) -> bool:
    """Print notes, conflicts and the whole plan. Returns False when there is nothing to do."""
    if plan.notes:
        _print_bullets(
            "Left as is (differs from the contract; this script never changes existing columns):",
            plan.notes,
            out,
        )
    if plan.conflicts:
        _print_bullets(
            "Needs a manual fix in Grist (this script changes nothing for these):",
            plan.conflicts,
            out,
        )
    if not new_doc and not plan.steps:
        print(
            ("\n" if args.guided else "")
            + "Nothing to do: the document already has everything this script adds. "
            "To verify it against the contract, run bestyet-init --check.",
            file=out,
        )
        return False
    _section(out, "Plan")
    if new_doc:
        _print_groups(_new_document_groups(plan, new_doc), out)
    else:
        _print_groups(grouped(plan.steps), out)
    if args.dry_run:
        print("Dry run: nothing was changed. Run again without --dry-run to apply.", file=out)
    return True


def _finish(
    plan: Plan,
    warnings: Sequence[str],
    args: argparse.Namespace,
    out,
    doc_id: str | None = None,
    applied: Sequence[Step] | None = None,
    doc: tuple[str, str] | None = None,
) -> None:
    """Print the closing message, warnings and next steps (`applied` None: nothing applied)."""
    if applied is not None:
        print(f"\n{done_message(applied, doc)}", file=out)
    if warnings:
        fix = next_step(args.allow_existing_tables)
        _print_bullets("Needs attention:", [f"{w}. To fix: {fix}" for w in warnings], out)
    if plan.conflicts:
        raise InitError(
            "The document differs from the contract in ways this script does not change "
            "(listed above). Fix them in Grist, then re-run."
        )
    if applied is not None:
        print_next_steps(
            out,
            doc_id,
            args.old_doc_id,
            events_hint=args.events_hint,
            implements_hint=args.implements_hint,
        )
