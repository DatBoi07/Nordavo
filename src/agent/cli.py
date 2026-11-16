"""CLI command interface for the Roosterly support agent.

Commands:
  python -m agent run [--dry-run]
  python -m agent eval [--tag NAME] [--repeats N]
  python -m agent status
  python -m agent reset-and-run
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
from typing import Sequence

from dotenv import load_dotenv

load_dotenv()

from agent.agent import SupportAgent, TicketDecision
from agent.faq import FAQStore
from agent.helpdesk import AlreadyHandled, HelpdeskClient, HelpdeskError
from agent.llm import LLMClient, PROMPT_VERSION
from agent.logging_setup import RunLogger
from agent.report import generate_review_report
from agent.state import DECIDED, FAILED, PENDING, SENDING, SENT, StateStore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("agent.cli")


def _compute_content_hash(ticket: dict) -> str:
    """Hash ticket content to enable deterministic caching."""
    key = f"{ticket.get('id', '')}:{ticket.get('subject', '')}:{ticket.get('body', '')}:{ticket.get('customer_plan', '')}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def run_command(dry_run: bool = False) -> int:
    """Execute the ticket agent across all open tickets."""
    print("=" * 60)
    print(f"ROOSTERLY SUPPORT TICKET AGENT ({'DRY RUN' if dry_run else 'LIVE RUN'})")
    print("=" * 60)

    helpdesk = HelpdeskClient()
    state = StateStore()
    faq_store = FAQStore()
    llm = LLMClient()
    agent = SupportAgent(faq_store=faq_store, llm_client=llm)
    run_logger = RunLogger()

    # 1. Fetch open tickets
    print("Fetching open tickets from helpdesk API...")
    try:
        open_tickets = helpdesk.get_tickets(status_filter="open")
    except Exception as e:
        logger.error("Failed to fetch tickets: %s", e)
        return 1

    print(f"Found {len(open_tickets)} open ticket(s).")
    if not open_tickets:
        print("No open tickets to process.")
        return 0

    decisions: list[TicketDecision] = []
    failures: list[str] = []

    # 2. Process each ticket sequentially
    for i, ticket in enumerate(open_tickets, start=1):
        ticket_id = ticket["id"]
        content_hash = _compute_content_hash(ticket)
        print(f"\n[{i}/{len(open_tickets)}] Processing Ticket {ticket_id}: '{ticket.get('subject', '')[:40]}'...")

        # Reconcile with local SQLite state
        local_rec = state.get_ticket(ticket_id)
        if local_rec and local_rec["status"] == SENT and not dry_run:
            logger.warning("Ticket %s was marked SENT locally but is OPEN in API. Reset detected, re-opening locally.", ticket_id)
            state.clear_ticket(ticket_id)
            local_rec = None

        decision: TicketDecision | None = None

        # Check if already decided
        if local_rec and local_rec["status"] in (DECIDED, SENDING) and local_rec.get("decision_json"):
            try:
                cached_data = json.loads(local_rec["decision_json"])
                print(f"  -> Reusing previously decided decision for {ticket_id}")
                decision = TicketDecision(**cached_data)
            except Exception:
                decision = None

        # Process through agent if not cached
        if decision is None:
            state.ensure_ticket(ticket_id, content_hash=content_hash)
            decision = agent.process_ticket(ticket)
            if not dry_run:
                # Cache decision locally
                state.store_decision(
                    ticket_id,
                    decision.__dict__,
                    content_hash=content_hash,
                    prompt_version=PROMPT_VERSION,
                    model=llm._model,
                )

        decisions.append(decision)
        payload = decision.to_api_payload()
        print(f"  Decision: {decision.action.upper()} | {decision.category} | {decision.priority} | conf: {decision.confidence}")

        # Post action if not dry run
        outcome = "dry_run"
        http_status: int | None = None
        attempts = 1

        if dry_run:
            print(f"  [DRY RUN] Would POST to /tickets/{ticket_id}/{decision.action}")
        else:
            state.mark_sending(ticket_id)
            try:
                if decision.action == "reply":
                    resp = helpdesk.reply(ticket_id, payload)
                elif decision.action == "handoff":
                    resp = helpdesk.handoff(ticket_id, payload)
                elif decision.action == "close":
                    resp = helpdesk.close(ticket_id, payload)
                else:
                    raise ValueError(f"Unknown action: {decision.action}")

                http_status = 200
                outcome = "sent"
                state.mark_sent(ticket_id)
                print(f"  -> Successfully sent {decision.action} for {ticket_id}")

            except AlreadyHandled:
                logger.warning("Ticket %s was already handled on server (409). Marking sent.", ticket_id)
                outcome = "already_handled"
                http_status = 409
                state.mark_sent(ticket_id)

            except Exception as e:
                logger.error("Failed to POST action for %s: %s", ticket_id, e)
                outcome = "failed"
                state.mark_failed(ticket_id, str(e))
                failures.append(ticket_id)

        # Log ticket outcome
        run_logger.log_ticket(
            ticket_id=ticket_id,
            language=decision.language,
            category=decision.category,
            priority=decision.priority,
            action=decision.action,
            confidence=decision.confidence,
            faq_ids=decision.faq_ids,
            flags=decision.flags,
            regex_signals=decision.regex_signals,
            http_status=http_status,
            attempts=attempts,
            llm_calls=decision.llm_calls,
            tokens_in=decision.tokens_in,
            tokens_out=decision.tokens_out,
            cost_eur=decision.cost_eur,
            latency_ms=decision.latency_ms,
            outcome=outcome,
            reason=decision.reason,
        )

    # 3. Generate review report
    report_file = "reports/review.md"
    generate_review_report(
        decisions,
        output_path=report_file,
        run_tag="Dry Run" if dry_run else "Live Run",
    )
    print(f"\nReview report generated at: {report_file}")
    print(f"Log written to: {run_logger.log_file}")

    # 4. Print Summary
    _print_run_summary(decisions, failures, llm, dry_run=dry_run)
    state.close()
    return 1 if failures else 0


def _print_run_summary(
    decisions: list[TicketDecision],
    failures: list[str],
    llm: LLMClient,
    dry_run: bool,
):
    print("\n" + "=" * 60)
    print("RUN SUMMARY")
    print("=" * 60)
    total = len(decisions)
    actions: dict[str, int] = {}
    for d in decisions:
        actions[d.action] = actions.get(d.action, 0) + 1

    print(f"Mode: {'DRY RUN' if dry_run else 'LIVE RUN'}")
    print(f"Total processed: {total}")
    for act, count in actions.items():
        print(f"  {act.upper():10s}: {count}")
    print(f"Failures: {len(failures)}")
    if failures:
        print(f"  Failed ticket IDs: {', '.join(failures)}")

    usage = llm.usage_summary()
    print(f"\nLLM Calls: {usage['call_count']}")
    print(f"Tokens In: {usage['total_tokens_in']:,}")
    print(f"Tokens Out: {usage['total_tokens_out']:,}")
    print(f"Total Cost: €{usage['cost_eur']:.4f}")
    if total > 0:
        cost_per_100 = (usage["cost_eur"] / total) * 100
        print(f"Estimated Cost per 100 tickets: €{cost_per_100:.4f}")
    print("=" * 60)


def status_command() -> int:
    """Print current helpdesk API status and local SQLite state."""
    print("=== HELPDESK API STATUS ===")
    helpdesk = HelpdeskClient()
    try:
        st = helpdesk.status()
        print(json.dumps(st, indent=2))
    except Exception as e:
        print(f"Error fetching helpdesk status: {e}")

    print("\n=== LOCAL STATE (state/agent.db) ===")
    state = StateStore()
    all_rows = state.all_tickets()
    counts: dict[str, int] = {}
    for r in all_rows:
        s = r["status"]
        counts[s] = counts.get(s, 0) + 1

    print(f"Total recorded tickets: {len(all_rows)}")
    for status, count in counts.items():
        print(f"  {status}: {count}")
    state.close()
    return 0


def reset_and_run_command() -> int:
    """Reset all practice tickets and execute the final scored run."""
    print("=" * 60)
    print("WARNING: POST /practice/reset will reset all 30 practice tickets.")
    print("The subsequent run will handle all tickets and cannot be undone.")
    print("=" * 60)
    confirm = input("Type 'RESET' to confirm and proceed: ").strip()
    if confirm != "RESET":
        print("Cancelled.")
        return 1

    print("\nResetting practice tickets via API...")
    helpdesk = HelpdeskClient()
    try:
        res = helpdesk.reset()
        print("API reset response:", res)
    except Exception as e:
        print("Failed to reset tickets:", e)
        return 1

    print("Clearing local state database...")
    state = StateStore()
    state.clear_all()
    state.close()

    print("Starting clean LIVE run...")
    return run_command(dry_run=False)


def eval_command(tag: str = "baseline", repeats: int = 1) -> int:
    """Run evaluation on GET /examples."""
    from evals.run_eval import run_eval_suite
    return run_eval_suite(tag=tag, repeats=repeats)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent", description="Roosterly Support Ticket Agent")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # run
    run_parser = subparsers.add_parser("run", help="Run the agent over open tickets")
    run_parser.add_argument("--dry-run", action="store_true", help="Inspect and draft without POSTing to API")

    # status
    subparsers.add_parser("status", help="Show API and local database status")

    # reset-and-run
    subparsers.add_parser("reset-and-run", help="Reset practice tickets and execute final run")

    # eval
    eval_parser = subparsers.add_parser("eval", help="Run evaluation harness on examples")
    eval_parser.add_argument("--tag", default="baseline", help="Tag name for eval results")
    eval_parser.add_argument("--repeats", type=int, default=1, help="Number of repetitions to check stability")

    args = parser.parse_args(argv)

    if args.command == "run":
        return run_command(dry_run=args.dry_run)
    elif args.command == "status":
        return status_command()
    elif args.command == "reset-and-run":
        return reset_and_run_command()
    elif args.command == "eval":
        return eval_command(tag=args.tag, repeats=args.repeats)

    return 0


if __name__ == "__main__":
    sys.exit(main())
