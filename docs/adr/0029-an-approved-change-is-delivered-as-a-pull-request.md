# 0029 - An approved change is delivered as a pull request

## Context

[ADR-0022](0022-the-specialist-deployment-is-a-second-compose-file.md) put `PUBLISH_MODE: branch`
in the specialist overlay, and a CI step asserts the overlay still says so. That was the right first
step: it moved delivery from *nothing survives the run* to *a branch survives the run*, on a
credential widened no further than pushing beside a default branch.

Four runs then delivered under it, and the result is what prompts this record. The output repository
holds four `agentic-patch/*` branches and **zero pull requests**. Each branch is one commit carrying
a whole generated project — `ahead=1, behind=0` against a default branch holding only a licence, a
readme and a `.gitignore`. So they are not increments to accumulate; they are four alternatives, of
which only the newest is wanted, and two of the older ones carry defects later releases fixed.

A bare branch is a delivery mechanism, not a review surface. It renders no diff anyone opens, runs
no checks, and carries none of the context a reviewer needs — most importantly, that this
particular pipeline's differential verdict is `mismatched` **by design**, because the program's own
unreachable end-of-file branch is faithfully reproduced. A reviewer meeting that number without
that sentence draws the wrong conclusion, and a branch has nowhere to put the sentence.

## Decision

**The specialist overlay sets `PUBLISH_MODE: pull_request`.**

`publish_change` already implements it: push the branch, then open a pull request from it against
the routed `output_repository`'s `output_branch`. The committed default stays `none`
(ADR-0021 § 5); this overlay remains the deliberate opt-in, which is the whole reason it is a
separate file.

**It still never writes to the default branch.** A pull request is a proposal. Merging stays a human
act, and the credential is not widened to perform one.

**The CI assertion moves with it.** The step named *"assert the specialist deployment override says
what ADR-0022 says it says"* asserted the literal string `PUBLISH_MODE: branch`. Left alone it fails
this change — which is the guard working, not a nuisance. It now asserts `pull_request`, and the
guard's value is unchanged: the overlay cannot drift from the record silently.

## Consequences

**The PAT needs a fourth grant, and this is the sentence that would otherwise be missing.** ADR-0022
enumerated three: `contents: read` on the specialist's repository for the build-time install,
`contents: read and write` on the routed output repository, and whatever the eventbus contract build
needed. Opening a pull request needs **`pull requests: write`** on the output repository as well.

It is easy to miss for the same reason ADR-0022's build-time grant was: it is not implied by the
others. `contents: write` is sufficient to push a branch and insufficient to open a pull request,
and a fine-grained token reports neither — the `permissions` block the API returns describes the
*repository role*, not the token's grants, so it cannot be used to confirm this before the fact.

**Enabling it before that grant is confirmed is safe by construction, and that is deliberate.**
`publish_change` pushes the branch *before* the API call. A refused pull request returns a partial
success carrying the branch name, logs the reason, and the run still reaches `completed`. The worst
case is the previous behaviour plus an error line; the work is never lost. So the first run under
this mode is the thing that proves the grant, and no separate probe is required.

**On a non-GitHub remote this degrades to `branch`** with a logged reason rather than failing the
run, unchanged from the module's original contract.

**Branches already delivered do not gain pull requests retroactively.** The mode is read at publish
time, so it decides only for runs published after the deployment restarts. The existing branches
want opening by hand or leaving; this record does not do it for them.

**`publish.py`'s description of the mode was wrong and is corrected here.** It said the pull request
opens against *"the branch the run cloned"*. `publish_node` passes `state.output_repository` and
`state.output_branch`, both of which come from routing rather than from the clone, and a deployment
that routes output away from the repository it read would have sent a reader to the wrong place. The
correction is phrased without naming any deployment's repositories, because this package is
domain-agnostic and CI asserts it carries no tenant vocabulary — the first attempt at this sentence
named them and was caught by that gate.
