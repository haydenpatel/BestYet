"""Init script questions: the guided prompts, and the workspace and document lookup they use.

Nothing is changed here: the answers fill in the arguments the CLI then acts on.
"""

import argparse
from collections.abc import Sequence

from bestyet.config import Config
from bestyet.grist import GristClient, GristError
from bestyet.init_errors import InitError, Interrupted, _explaining, explain
from bestyet.init_output import _and, _section
from bestyet.init_plan import implement_event_names
from bestyet.init_steps import (
    _event_names,
    _non_empty,
    _norm,
    contract_table_ids,
    unrelated_tables,
)

# Duplicate names.


class NameTaken(InitError):
    """A document with that name already exists in the workspace; a second is never created."""

    def __init__(self, existing: str, label: str):
        super().__init__(
            f"A document named {existing!r} already exists in {label}. Pick another name with "
            "--name, or run without --create using GRIST_DOC_ID to set that one up."
        )


def existing_docs(client: GristClient, workspace_id: int) -> list[dict]:
    """The documents in one workspace, as Grist lists them (`id` and `name`)."""
    for org in client.orgs():
        for w in client.workspaces(org["id"]):
            if w["id"] == workspace_id and "docs" in w:
                return list(w["docs"])
    return list(client.workspace(workspace_id).get("docs", []))


def name_clash(
    client: GristClient, workspace_id: int, name: str, docs: Sequence[dict] | None = None
) -> dict | None:
    """The document in the workspace that has this name (ignoring case and surrounding
    spaces), if any. `docs`: a listing already read (None: read it now)."""
    wanted = name.strip().casefold()
    if docs is None:
        docs = existing_docs(client, workspace_id)
    return next((d for d in docs if d["name"].strip().casefold() == wanted), None)


def _workspace(admin: GristClient, args: argparse.Namespace) -> tuple[int, str]:
    """The workspace's (id, label), looked up once per run."""
    if args.ws_resolved is None:
        args.ws_resolved = resolve_workspace_label(admin, args.workspace, args.org)
    return args.ws_resolved


def _workspace_docs(admin: GristClient, args: argparse.Namespace) -> list[dict]:
    """The workspace's documents, listed once per run (a final re-check lists again)."""
    if args.ws_docs is None:
        args.ws_docs = existing_docs(admin, _workspace(admin, args)[0])
    return args.ws_docs


# Workspace lookup.


def _orgs(client: GristClient, org: str | None) -> list[dict]:
    """The organisations the key can see, narrowed to `org` (an id or name) when given."""
    orgs = client.orgs()
    if org is not None:
        orgs = [o for o in orgs if str(o["id"]) == org or o.get("name") == org]
        if not orgs:
            raise InitError(ORG_NOT_FOUND.format(org=org))
    return orgs


def resolve_workspace_label(
    client: GristClient, workspace: str, org: str | None = None
) -> tuple[int, str]:
    """Return (workspace id, a label for messages): its name when it can be found."""
    if workspace.isascii() and workspace.isdigit():
        try:  # best effort: only for the plan's wording
            for o in client.orgs():
                for w in client.workspaces(o["id"]):
                    if str(w["id"]) == workspace and w.get("name"):
                        return int(workspace), f"workspace {w['name']!r}"
        except GristError:
            pass
        return int(workspace), f"workspace {workspace}"
    orgs = _orgs(client, org)
    matches = [w for o in orgs for w in client.workspaces(o["id"]) if w.get("name") == workspace]
    if not matches:
        raise InitError(
            f"Workspace {workspace!r} not found. Check --workspace (an id or exact name, "
            "with --org if the name is used in several organisations) and that the API key's "
            "user can see it."
        )
    if len(matches) > 1:
        ids = ", ".join(str(w["id"]) for w in matches)
        raise InitError(
            f"Workspace name {workspace!r} is ambiguous (ids: {ids}); use the id or --org"
        )
    return int(matches[0]["id"]), f"workspace {workspace!r}"


ABORT = "aborted; nothing was changed"


ORG_NOT_FOUND = (
    "Organisation {org!r} not found. Check --org (an id or exact name) and that the API "
    "key's user can see it."
)


def _ask(out, prompt: str, stdin) -> str:
    out.write(prompt)
    out.flush()
    try:
        line = stdin.readline()
    except KeyboardInterrupt:
        print(file=out)
        raise Interrupted(ABORT) from None
    if not line:  # end of input
        print(file=out)
        raise Interrupted(ABORT)
    return line.strip()


def _ask_yes_no(out, question: str, stdin, default: bool = False) -> bool:
    hint = "[Y/n]" if default else "[y/N]"
    while True:
        answer = _ask(out, f"{question} {hint} ", stdin).lower()
        if not answer:
            return default
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        print("Please answer y or n.", file=out)


def _ask_choice(
    out, question: str, options: Sequence[str], stdin, create_option: bool = False
) -> int | None:
    """Numbered list; returns the 0-based index chosen (None for C with `create_option`)."""
    print(question, file=out)
    for i, option in enumerate(options, 1):
        print(f"  {i}. {option}", file=out)
    prompt = f"Enter a number (1-{len(options)})"
    prompt += ", or C to create a new document: " if create_option else ": "
    while True:
        answer = _ask(out, prompt, stdin)
        if create_option and answer.lower() in ("c", "create"):
            return None
        if answer.isascii() and answer.isdigit() and 1 <= int(answer) <= len(options):
            return int(answer) - 1
        print(
            f"Please enter a number from 1 to {len(options)}{', or C' if create_option else ''}.",
            file=out,
        )


def _choose_workspace(client: GristClient, org: str | None, out, stdin) -> str | None:
    """Offer the visible workspaces; returns the chosen id, or None when the only one is
    declined (a lone workspace is never taken silently)."""
    orgs = _orgs(client, org)
    several = len(orgs) > 1
    found = [(o, w) for o in orgs for w in client.workspaces(o["id"])]
    if not found:
        raise InitError(
            "The API key cannot see any workspace to put a document in. Check GRIST_API_KEY, "
            "or ask for access to a workspace in Grist."
        )
    if len(found) == 1:
        name = found[0][1].get("name", found[0][1]["id"])
        if _ask_yes_no(out, f"Use workspace {str(name)!r}?", stdin, default=True):
            return str(found[0][1]["id"])
        return None
    labels = []
    for o, w in found:
        label = str(w.get("name", w["id"]))
        labels.append(f"{label} ({o.get('name', o['id'])})" if several else label)
    question = "Which workspace do you want to use?"
    return str(found[_ask_choice(out, question, labels, stdin)][1]["id"])


def guide(args: argparse.Namespace, config: Config, client_for, out, stdin) -> bool:
    """Ask what to set up and fill in `args` (a given flag skips its question); returns False
    when the user stops. Nothing is changed here."""
    _section(out, "Questions")
    if not args.yes:
        print("Nothing is changed until you confirm the plan.\n", file=out)
    doc_id = None
    create = args.create
    if config.grist_doc_id and not create and not args.workspace and not args.org:
        name = None
        try:
            with client_for(config.grist_doc_id) as client:
                name = client.document().get("name", "")
        except GristError as e:  # a stale or unreadable id must not stop the questions
            print(
                f"GRIST_DOC_ID is set but that document cannot be read. "
                f"{explain(e, config, 'document')} Choose another document below.\n",
                file=out,
            )
        if name is not None:
            question = f"GRIST_DOC_ID is set to the document {name!r}. Set up that document?"
            if _ask_yes_no(out, question, stdin, default=True):
                doc_id = config.grist_doc_id
    if doc_id is None:
        with _explaining(config, "workspace"), client_for("") as admin:
            if not args.workspace:
                args.workspace = _choose_workspace(admin, args.org, out, stdin)
                if args.workspace is None:
                    print(
                        "There is no other workspace visible to this API key. Create one in "
                        "Grist, then run this again. Nothing was changed.",
                        file=out,
                    )
                    return False
            ws_id, label = _workspace(admin, args)
            if not create:  # which document: one that is already there, or a new one
                docs = _workspace_docs(admin, args)
                if not docs:
                    create = True
                else:
                    pick = _ask_choice(
                        out,
                        f"Do you want to use an existing document in {label}?",
                        [d["name"] for d in docs],
                        stdin,
                        create_option=True,
                    )
                    create = pick is None
                    if pick is not None:
                        doc_id = docs[pick]["id"]
            if create:
                while True:
                    given = bool(args.name)
                    name = args.name or _ask(out, "Name for the new document [BestYet]: ", stdin)
                    args.name = name or "BestYet"
                    clash = name_clash(admin, ws_id, args.name, _workspace_docs(admin, args))
                    if not clash:
                        break
                    question = (
                        f"A document named {clash['name']!r} already exists in this workspace. "
                        "Use it instead?"
                    )
                    if _ask_yes_no(out, question, stdin, default=True):
                        create, doc_id = False, clash["id"]  # set that one up (it only adds)
                        break
                    if given:
                        print("Nothing was changed.", file=out)
                        return False
                    args.name = None
    args.create = create
    demo_allowed = True
    present_events: set[str] = set()  # a new document has no Events; an existing one may
    if not create:
        args.doc_id = doc_id
        with _explaining(config, "document"), client_for(doc_id) as client:
            tables = client.tables()
            extra = unrelated_tables(tables)
            if extra and not args.allow_existing_tables:
                print(
                    f"This document has tables that are not part of BestYet ({', '.join(extra)}). "
                    "The script can still add its own tables next to them.",
                    file=out,
                )
                if not _ask_yes_no(out, "Continue and add to it anyway?", stdin):
                    print("Nothing was changed.", file=out)
                    return False
                args.allow_existing_tables = True
            demo_allowed = not _non_empty(client, contract_table_ids(tables))
            if any(t["id"] == "Events" for t in tables):
                present_events = {_norm(n) for n in _event_names(client.records("Events")).values()}
    if args.yes or args.dry_run:  # only a required value was missing: flag defaults apply
        return True
    if demo_allowed and not args.demo_data:
        args.demo_data = _ask_yes_no(
            out,
            "Add demo data (a few fictional athletes, meets and results, with the events and "
            "sample implements they use)?",
            stdin,
        )
    if not args.demo_data:  # demo data covers the Events and Implements it needs
        if not args.seed_events:
            args.seed_events = _ask_yes_no(
                out, "Add default events (standard track and field list)?", stdin
            )
        if not args.seed_implements:
            question = "Add sample implement weights (by grade and division)?"
            names = implement_event_names()
            if not args.seed_events and any(_norm(n) not in present_events for n in names):
                # Yes would add events that are not otherwise being added: say so, after the
                # question and before the answer.
                kind = "events" if len(names) > 1 else "event"
                print(question, file=out)
                question = (
                    f"This also adds the {_and(list(names))} {kind}, which the implements use."
                )
            args.seed_implements = _ask_yes_no(out, question, stdin)
    return True
