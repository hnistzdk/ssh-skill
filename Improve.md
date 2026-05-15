# SSH Skill UX, Verification, and Runtime Unification Design

Date: 2026-04-09

## Summary

This design improves `ssh-skill` based on the real friction exposed during the `172.20.179.136` remote validation workflow. The current scripts already support direct targets and config reuse, but the end-to-end experience is still awkward in Windows shells, inconsistent across execute/upload/download/tunnel flows, and too opaque for long-running remote verification tasks.

The goal is not to redesign SSH itself. The goal is to make the existing toolset easier to drive, easier to trust, and easier to extend by introducing a phased unification of CLI behavior, runtime reporting, verification semantics, and internal execution layers.

This work should preserve current script entrypoints and backward-compatible happy paths while reducing the amount of shell-specific knowledge and manual post-checking required from the user.

## Goals

- Reduce unreasonable or inconvenient steps in the current SSH validation workflow.
- Keep existing script entrypoints (`ssh_execute.py`, `ssh_upload.py`, `ssh_download.py`, `ssh_tunnel.py`) while making their behavior more consistent.
- Absorb Windows/MSYS path and quoting sharp edges into the tool layer as much as possible.
- Improve observability for long-running remote commands and regeneration workflows.
- Add a minimal built-in verification model so success can mean more than "the command returned 0".
- Standardize success/error/result output across single-host operations.
- Refactor duplicated logic into shared layers without changing the external mental model more than necessary.
- Add regression coverage for the exact OpenAPI replacement/regeneration/validation workflow that exposed the current pain points.

## Non-Goals

- No TUI or interactive terminal UI.
- No workflow orchestration platform or job scheduler.
- No automatic guessing of project-specific commands or deployment paths.
- No replacement of current scripts with a single mandatory monolithic command.
- No daemon protocol redesign.
- No migration away from current config metadata storage in this phase.
- No attempt to solve every multi-host or cluster use case as part of this design.

## Current Problems

### 1. Windows shell friction leaks into normal usage

The current toolset still expects the caller to know details like `MSYS_NO_PATHCONV=1`, remote path quoting, and which shell behaviors are unsafe. That means users must carry platform-specific operational knowledge that should mostly live inside the skill.

### 2. Single-host operations feel like separate tools, not one system

`execute`, `upload`, `download`, and `tunnel` share concepts like target resolution, connection overrides, persistence policy, and reporting, but each script exposes these a little differently. This increases cognitive load and makes cross-command workflows feel brittle.

### 3. Long-running remote commands are too opaque

When a regeneration or validation command takes time, the current experience makes it hard to tell:

- what target was actually used
- what working directory or shell context was used
- whether the command is still running normally
- whether failure happened in transport, command execution, or downstream artifact validation

### 4. "Success" is too weak for verification-heavy workflows

In the OpenAPI workflow, command success alone is not enough. The real question is whether specific remote artifacts were produced and whether their structure matches expectations. Today that verification is manual and external to the command model.

### 5. Internal duplication makes behavior drift likely

The skill already centralizes some target resolution behavior, but important pieces are still spread across entrypoints. This makes it harder to improve one flow without forgetting another.

## Design Principles

1. **Keep the external surface familiar.** Existing scripts remain valid entrypoints.
2. **Centralize shared semantics.** Resolution, normalization, execution reporting, verification, and error classification should not be reimplemented per script.
3. **Bias toward explicitness over magic.** The tool should expose what it did, but users should not need to remember shell trivia.
4. **Make success meaningful.** For workflows that care about generated files or content structure, verification should be first-class.
5. **Refactor only where it improves the current workflow.** No speculative architecture.

## User-Facing Design

## 1. Unified CLI behavior for single-host operations

All single-host scripts should share a consistent parameter model where applicable.

### Shared connection options

- `--alias <name>`
- `--user <user>`
- `--port <port>`
- `--password <password>`
- `--key <path>`
- `--description <text>`
- `--environment <env>`
- `--tags <csv>`
- `--location <text>`
- `--no-persist`

These already exist in parts of the system; the change is to make the supported set and semantics consistent.

### Shared reporting options

- `--json` — force machine-readable structured output
- `--quiet` — suppress non-essential progress text
- `--verbose` — include richer execution context and decision details

### Shared runtime context options

- `--cwd <remote-dir>` — run a remote command from a specific working directory when supported by the operation
- `--shell <sh|bash|cmd|powershell>` — make shell choice explicit for command execution when needed

`--cwd` and `--shell` matter mostly for `ssh_execute.py`, but the output model should still expose runtime context consistently.

## 2. Verification options for execution flows

Add a minimal verification layer that can be used after the main remote command succeeds.

Initial verification capabilities:

- `--verify-exists <remote-path>`
- `--verify-read <remote-path>`
- `--verify-grep <remote-path>::<pattern>`
- `--verify-command "<command>"`

These checks should run only after the primary operation succeeds. They are not meant to replace general scripting; they are meant to cover the common "did the expected thing really happen" cases.

### Example target workflow

A future flow like the OpenAPI regeneration scenario should be representable as:

1. upload changed file
2. run minimal regeneration command with `--cwd`
3. verify generated YAML exists
4. read or grep key structure indicators

This keeps the verification intent close to the command that produced the artifact.

## 3. Better command observability

For long-running commands, the result should clearly report:

- resolved target and alias
- resolution mode (`existing` or `direct`)
- whether persistence happened
- remote working directory, when used
- shell, when explicitly set
- command string
- duration
- exit code
- verification results

Verbose mode should also expose relevant resolution decisions, such as whether the target matched by alias or by `HostName` reverse lookup.

## Output Model

All single-host operations should converge on one structured result shape.

### Proposed top-level result

```json
{
  "success": true,
  "operation": "execute",
  "target": "172.20.179.136",
  "alias": "server-172-20-179-136",
  "mode": "existing",
  "matched_by": "hostname",
  "persisted": false,
  "result": {},
  "verification": {
    "success": true,
    "checks": []
  },
  "error": null
}
```

### Operation-specific `result`

- `execute`
  - `command`
  - `cwd`
  - `shell`
  - `stdout`
  - `stderr`
  - `exit_code`
  - `duration_ms`
- `upload`
  - `local_path`
  - `remote_path`
  - `bytes_transferred`
  - `duration_ms`
- `download`
  - `remote_path`
  - `local_path`
  - `bytes_transferred`
  - `duration_ms`
- `tunnel`
  - `tunnel_id`
  - `local_port`
  - `remote_host`
  - `remote_port`

### Verification block

```json
{
  "success": true,
  "checks": [
    {
      "type": "exists",
      "target": "/root/mcloud-openapi/mcloud-api.yaml",
      "success": true
    },
    {
      "type": "grep",
      "target": "/root/mcloud-openapi/mcloud-api.yaml",
      "pattern": "AuthorizationHeader: \\{\\}",
      "success": true
    }
  ]
}
```

This makes validation-heavy workflows auditable without requiring external interpretation of raw output alone.

## Error Model

Standardize errors across entrypoints using a small, explicit taxonomy.

### Proposed error codes

- `cli_argument_error`
- `path_normalization_error`
- `target_resolution_error`
- `auth_error`
- `connection_error`
- `transport_error`
- `remote_command_error`
- `timeout_error`
- `verification_error`
- `internal_error`

### Error structure

```json
{
  "code": "verification_error",
  "message": "Expected grep pattern was not found in remote file",
  "details": {
    "target": "/root/mcloud-openapi/mcloud-api.yaml",
    "pattern": "AuthorizationHeader: \\{\\}"
  }
}
```

### Why this matters

Today, several failure modes collapse into generic failure text. In real workflows, users need to know whether to fix arguments, credentials, target resolution, remote command content, or post-command artifact expectations.

## Windows and Shell Compatibility Design

## 1. Central path normalization rules

Introduce a shared normalization layer that all relevant entrypoints use before execution.

Responsibilities:

- detect obvious MSYS-converted remote paths
- detect obviously wrong local/remote path direction
- normalize safe path forms where possible
- produce one consistent error shape when normalization fails

## 2. Reduce user dependence on `MSYS_NO_PATHCONV=1`

The docs can still mention `MSYS_NO_PATHCONV=1`, but the implementation should treat it as a fallback compatibility detail, not as user knowledge required for routine success.

Target behavior:

- if a path is already correct, accept it
- if a path was obviously mangled by shell conversion, return a precise normalization error
- where safe, repair known path forms before invoking the transfer

The important shift is that the system should own more of the compatibility burden.

## 3. Make shell context explicit in results

If a command depends on `bash` semantics, the caller should be able to say so, and the result should report it. This avoids ambiguity in workflows that currently rely on implicit shell assumptions.

## Internal Architecture

The internal design should be layered so behavior becomes easier to evolve without changing all scripts in parallel.

## 1. CLI layer

Each script remains an entrypoint but becomes thinner.

Responsibilities:

- parse operation-specific and shared arguments
- call shared normalization/resolution/execution helpers
- format final output

## 2. Resolution layer

Build on the existing config-centered target resolution from `config_v3.py`.

Responsibilities:

- resolve alias vs hostname/IP matches
- merge CLI overrides consistently
- determine persistence intent
- return a normalized resolved target object

This layer already exists in part; the main change is to make it the required path for all single-host operations.

## 3. Runtime layer

Introduce an operation runner abstraction shared by execute/upload/download/tunnel.

Responsibilities:

- execute the chosen operation
- collect duration and runtime metadata
- classify failures into the shared error model
- return an operation-specific result payload

This layer is where behavior should stop drifting between scripts.

## 4. Verification layer

A shared post-operation verification runner should evaluate requested checks after successful execution.

Responsibilities:

- run `exists`, `read`, `grep`, and `verify-command` checks
- collect structured results for each check
- convert failed checks into `verification_error`

## 5. Reporting layer

A shared output formatter should build the final result envelope.

Responsibilities:

- map runtime results into the unified top-level structure
- include resolution metadata
- include verification output
- enforce consistent JSON and human-readable formatting

## Phase Plan

## Phase 1: Fix the sharpest workflow pain

Scope:

- unify shared argument parsing for single-host operations
- add shared normalization/error classification for paths and shell context
- improve `ssh_execute.py` output with duration, cwd, shell, exit code, and resolution metadata
- add initial verification support for execute workflows

Why first:

This phase directly addresses the OpenAPI validation pain without requiring a full internal rewrite.

## Phase 2: Unify operation runtime behavior

Scope:

- introduce shared operation runner abstraction
- standardize result envelopes for upload/download/tunnel
- align persistence and resolution reporting across all single-host scripts
- expose fallback/runtime choice more clearly where native vs Paramiko behavior matters

Why second:

This phase turns the toolset from "similar scripts" into a coherent system.

## Phase 3: Complete refactor, docs, and regression hardening

Scope:

- split shared logic into stable modules
- align SKILL.md/README docs with the new model
- expand tests around Windows compatibility and verification
- add end-to-end regression coverage for the OpenAPI workflow

Why third:

This phase hardens maintainability and keeps docs truthful after behavior convergence.

## Testing Strategy

## Unit tests

Cover at least:

- target resolution behavior
- path normalization rules
- verification check parsing and execution
- error classification mapping
- result-envelope formatting

## Integration tests

Cover at least:

- execute with direct target and existing alias
- upload/download with Windows-style local paths and Unix remote paths
- execute with `--cwd`
- execute with verification checks
- tunnel start/list/status/stop using shared output semantics

## End-to-end regression scenario

Add one representative regression case modeled on the real workflow:

1. upload `RestDocumentationGenerator.groovy`
2. run the minimum necessary remote regeneration command
3. verify `/root/mcloud-openapi/mcloud-api.yaml` exists
4. verify `security` rendering contains single-line `- AuthorizationHeader: {}`
5. inspect top-level OpenAPI structure (`openapi`, `info`, `servers`, `components`, `security`, `paths`)

This scenario matters because it exercises the exact combination of transfer, remote execution, artifact validation, and structured verification that exposed current weaknesses.

## Acceptance Criteria

This design is successful when:

- users can perform the validation workflow without carrying shell-specific trivia in their head
- single-host scripts expose one consistent reporting and error model
- long-running command outcomes are understandable without extra guesswork
- verification-heavy workflows can be expressed directly in the tool invocation model
- the OpenAPI workflow becomes a stable regression scenario instead of a fragile manual procedure

## Trade-Offs

### Why not build one brand-new umbrella command now?

A brand-new umbrella CLI might be cleaner eventually, but it would create more migration cost and documentation churn than necessary. Keeping current entrypoints while unifying their internals is the safer path.

### Why not build full log streaming or orchestration?

The problem observed was not lack of a remote job platform. The problem was unclear outcomes and scattered verification. A lighter verification and reporting model solves the current need with less complexity.

### Why add verification into the SSH layer at all?

Because the real workflow already depends on verification. Keeping it entirely outside the tool forces users to manually reconstruct intent every time. A small built-in verification layer improves trust without turning the SSH layer into a business-specific automation system.

## Open Questions Resolved

- **Priority:** improve both usability and stability, in phases.
- **Depth:** do targeted UX fixes and internal refactoring together, not patch-only work.
- **Compatibility:** preserve current script entrypoints.
- **Scope boundary:** avoid TUI/orchestration; focus on the concrete friction points revealed by actual use.

## Recommended Implementation Order

1. extract shared argument and normalization helpers
2. extend execute flow with richer runtime reporting and verification
3. standardize result envelope and error model
4. migrate upload/download to shared helpers
5. migrate tunnel reporting to shared helpers
6. update docs and usage examples
7. add regression coverage for the OpenAPI remote validation scenario