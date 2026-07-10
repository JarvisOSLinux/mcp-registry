# Trust Levels

> **Superseded by [`TRUST-MODEL.md`](TRUST-MODEL.md).** The canonical model uses
> **two** tiers — `community` and `official` — plus the `deprecated`/`removed`
> revocation states, not the three-level scale below. `TRUST-MODEL.md` also
> defines the actor model (human CLI vs. autonomous agent) and the dmcp
> enforcement contract, neither of which is covered here. The review criteria and
> revocation process below remain accurate and now describe the `official` tier;
> read them through the two-tier mapping in `TRUST-MODEL.md` §3. Do not add new
> `trustStatus` values from this file.

Every server in the registry carries a `trustStatus` field in `registry.json`. This field signals how thoroughly the server has been reviewed and how much a user can trust its behavior.

Trust is not a binary flag. It is a progressive scale that balances openness (accepting new servers quickly) with safety (protecting users from malicious or broken servers).

---

## The Three Levels

### `unreviewed`

**Default for all new submissions.**

The server manifest has been accepted (valid JSON, passes CI checks, no obvious malware in the manifest itself), but no human has audited the source code, verified the license, or confirmed the server behaves as described.

Users who install an `unreviewed` server accept that:
- The source code has not been independently audited.
- The server may have bugs, security issues, or behave unexpectedly.
- The maintainers of this registry have not endorsed it.

`unreviewed` is appropriate for:
- Testing a server before it receives wider use.
- Internal or personal tools that do not need a public safety guarantee.
- Servers awaiting a community review cycle.

---

### `community`

**Reviewed by a maintainer or trusted community member.**

A human reviewer has:
1. Read the source code and found no obvious malicious behavior.
2. Confirmed the declared license is accurate.
3. Verified the server builds and runs as the manifest describes.
4. Checked that the `tools` descriptions accurately reflect what the tools do.
5. Confirmed no outbound network calls occur that are not declared in `trust.securityNotes`.

`community` does **not** mean:
- A formal security audit was performed.
- The server is free of all bugs.
- Cryptographic signatures were verified.

`community` is appropriate for general use by users who trust the JARVIS registry community.

---

### `verified`

**Deep review by a core maintainer.**

In addition to `community` criteria, a verified server has:
1. Source pinned to a specific commit or release tag (not just a branch).
2. The pinned source hash recorded in `trust.checks`.
3. A build verified from that exact commit to ensure the running artifact matches the source.
4. All `configurableProperties` and their handling audited for secret leakage.
5. Network access (if any) audited and limited to declared endpoints.
6. A formal note in the registry entry from the reviewer.

`verified` is suitable for security-sensitive deployments, enterprise use, or servers with access to credentials, filesystems, or privileged operations (e.g. `scope: "system"` servers).

---

## Trust Status in registry.json

The `trustStatus` field is set per server in `registry.json`:

```json
{
  "id": "com.github.alice.mcp.git-summary",
  "trustStatus": "community",
  ...
}
```

It is not set in `manifest.json` — the manifest is under the control of the submitter. The registry entry is under the control of the registry maintainers.

---

## Promotion Process

### Unreviewed → Community

1. The server has been in the registry for at least **48 hours** with no reported issues.
2. A maintainer or trusted contributor opens a review PR or leaves a review comment.
3. The reviewer checks the criteria listed above for `community`.
4. A maintainer updates `trustStatus` to `"community"` in `registry.json` and records the review in `trust.reviewReferences`.

To request community review, open a GitHub issue with the title:
```
[trust-review] com.github.<username>.mcp.<server-name>
```
Link the review issue from your original submission PR.

### Community → Verified

1. The submitter or a maintainer opens a verified-review request issue.
2. A core maintainer performs the deep review described above.
3. The source is pinned to a specific commit or tag and the hash is recorded.
4. `trustStatus` is updated to `"verified"` and the reviewer's GitHub handle is added to `trust.reviewReferences`.

Verified reviews are done on a best-effort basis. System-scope servers and servers handling credentials are prioritized.

---

## Revoking Trust

Trust can be downgraded or the server deprecated if:
- A security issue is discovered.
- The upstream repository is deleted, abandoned, or taken over.
- The server is found to behave contrary to its declared description.

Revoked servers are set to `trustStatus: "deprecated"` or `"removed"` and a note is added to `trust.notes`. They are not deleted from the registry immediately to avoid breaking existing installations, but dmcp will warn users about deprecated servers.

---

## Summary Table

| Level | Source read? | License verified? | Build verified? | Source pinned? | Suitable for |
|-------|-------------|-------------------|-----------------|----------------|--------------|
| `unreviewed` | No | No | No | No | Testing, personal use |
| `community` | Yes | Yes | Yes | No | General use |
| `verified` | Yes (deep) | Yes | Yes (from pin) | Yes | Security-sensitive, enterprise, system-scope |
