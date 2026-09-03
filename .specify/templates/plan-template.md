# Plan: <title>

**Spec:** [spec.md](spec.md)

## Approach

The shape of the change in a paragraph, and the alternative that was rejected with the
reason. "Considered and rejected" is more useful later than the choice alone.

## Risks

What breaks if this lands wrong. Name exact files, read rather than guessed: import paths,
Dockerfile `COPY` lines, compose mounts, CI paths, anything another repository depends on.

| Risk | Blast radius | Mitigation |
|---|---|---|

## Sequence

Ordered, each step independently committable and verifiable. If a step cannot be verified on
its own, it is not a step.

| # | Change | Verified by |
|---|---|---|
| 1 | | |

## Decisions needing an ADR

Anything here someone could reasonably have chosen otherwise.
