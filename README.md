# starter-apps/support-desk

An AgentBox custom application: **Python** + **claude-agent-sdk**, running the
**Support Desk** concept -- a support agent for a small shop that answers tickets,
looks customers up, records what it did and hands a case over when the house
rules say so (see *The concept* below). It is wired so that every AgentBox
control has something in it to actually exercise: a bundled skill, a bundled MCP
server, an agent with real tools, a governed package install and an outbound
call that policy decides on.

One app, one agentic SDK. That is deliberate: the previous combined starter
could not resolve its own npm peer dependencies and shipped a 998MB dependency
layer into every image. See `docs/custom_app_developer_guide.md` section 11.

## Open the console

The application serves its own page on its own port. Once the app reaches
*running*, open:

    http://127.0.0.1:8080/

The page is written for the people who show it, not for the people who built
it. One **Play** button, eight steps, one plain sentence per result, and at the
end a card that sums up what AgentBox did. Each step is a canned message sent
through the ordinary chat; the chat box at the bottom is the same pipe, so
anything you type gets the same treatment. Nothing on the page is technical
on purpose -- it says *your customer system*, *house rules*, *personal data*,
*going online*, *a manager's approval*, and never the names of the parts that
do the work.

Three things about that page are worth knowing before you show it to anyone.

**Every sentence is derived from a real signal.** The page never decides a
result from which button was pressed; it decides from what came back -- the
gateway's 403 code, the bridge's approval id, the proxy's status line, Package
Guard's verdict -- and then reads the sentence for that signal out of
`concept/concept.json`. A refusal is the control doing its job and renders
green. A control that *broke* renders grey, says so in the platform's own
words, and is never counted.

**The status pill is derived too.** *Protected by AgentBox*, *Partly protected*
or *Not protected* comes from `GET /runtime-info`, which reads what the
platform actually put in this container. There is no flag. Run this same image
with plain `docker run` and the pill goes red because the variables are absent,
not because anyone told it to.

**It needs no network.** No CDN, no analytics, and the two typefaces ship in
`ui/fonts/`. A demo that needs the internet cannot be given on a plane or in a
locked-down customer network.

### See it next to an unwrapped app

The simplest version of this needs nothing extra: run **this same image**
outside AgentBox with plain `docker run`, on any free port, and open its
console. Same routes, same page, and the banner is derived red instead of
green because the controls genuinely are not there.

**Pick a port nothing is using, and do not reuse 8080 or 8081.** Those are the
ports AgentBox applications themselves most often declare -- on a live install
`8081` is typically a *protected* app -- so putting the unwrapped copy there is
how the two windows get confused for each other. `9099` is a reasonable
choice precisely because nothing here suggests it means anything.

A fuller twin, with the vendor call going direct instead of through the
gateway, lives at `demos/unguarded-support-desk/` in the **AgentBox** repository
rather than in this one. It carries a byte-identical copy of the console, held
that way by a test. Either way the comparison needs a real vendor key in a
`.env`, which is itself the first thing being shown.

### Step 2 and tokenization

Step 2 is a refund to the customer's bank account, and its result depends on
one setting:

- **Off (the default):** the IBAN stops the message, and it never reaches the
  AI. The step reads **Stopped**.
- **On:** in the admin console, open Security → Services → gateway →
  Sensitive-data rules, set IBANs to *Use a placeholder*, allow
  `crm (support-desk) · issue_refund` to receive the real IBAN, and Save &
  apply. The AI now works with a placeholder. The refund waits for a manager
  (**Waiting**), because a refund is a financial action. After approval, only
  the refund tool gets the real number (**Done**), and the chat shows it as
  "IBAN ending 3000".

## Is this app protected?

**Not by its port.** A port number carries no posture and never has. On one
machine `8081` is an AgentBox-managed application with SecureProxy, egress
policy and Package Guard all wired; on another it is an unwrapped container
with none of them. Measured on two real installs on 2026-09-22, and the two
answers were opposite. Do not read protection into a port, a name, or which
window is on the left.

**The app answers for itself.** `GET /runtime-info` reports what AgentBox
actually put in this container -- the proxy variables, the CA, the bridge
URL and token, the skills mount, whether the package manager on `PATH` is a
Package Guard shim -- and the console's banner is derived from it. There is
no demo flag to set.

**The same image is protected or not depending on who runs it.** These
starters are not "secure repositories"; nothing in the source makes them safe.
Run this image under AgentBox and `/runtime-info` reports the controls it was
given. Run the identical image yourself with `docker run` and it reports them
absent, because they are. That is the whole comparison, and it needs no second
codebase -- which is why the banner is worth more than any badge this README
could carry.

## What the agent can do

The agent has tools, and every one of them goes through a platform control
and reports the verdict (`app/agent_tools.py`):

- **The company's systems.** At each request the app asks MCP Bridge which
  tools this application has been granted (`GET /tools`) and offers exactly
  those to the model, under their own names. A call goes through the bridge
  (`POST /call`); a destructive tool is *held* there and answered with an
  approval id, and the agent is told the action has not run and will not run
  by itself -- after a manager approves it in the console, the same request
  has to be made again.
- **`post_to_site`** -- an outbound HTTP POST. Inside AgentBox the only route
  out is SecureProxy's forward proxy, which answers 403 for any host not on
  this application's list; the tool asks it first and sends nothing on a
  refusal.
- **`install_package`** -- `pip install` through Package Guard's shim:
  allow, block, or hold for an operator.

Claude Code's own built-in tools are switched off; the agent reaches nothing
except through these. `POST /process/stream` emits a `tool` event for each
call and a `tool_result` event carrying a *signal* (`tool_ok`, `tool_held`,
`tool_denied`, `egress_blocked`, `egress_ok`, `pkg_allow`, `pkg_block`,
`pkg_hold`, `failed`) derived from what actually came back -- the bridge's
body, the proxy's status line, the guard's verdict -- never from what was
asked. Run this image outside AgentBox and the same tools still exist; the
signals then say that nothing checked anything.

## Take the tour

> **The "Delete old customer records" step waits for a manager because AgentBox enforces sensitive-call approvals by default.** If an admin has switched **Automation › Approvals** to *Monitor*, the call is still classified and written to the audit log but it runs; switch back to **Enforce** before the tour and the destructive call is held with an approval id.

Add this application in the admin console (**Applications -> Add application**,
by Git URL or ZIP upload), wait for it to reach *running*, then press **Try
API** on its card. Each step below is one preset, in order, and names what to
look at afterwards. The whole thing takes about ten minutes.

**1. Health check** -- `GET /health`. The app answers. Nothing else to see; it
is here so a failure at step 2 is clearly not "the app is down".

**2. Agent call (via SecureProxy)** -- `POST /process`. A real agent run. The
app holds no provider key: AgentBox injected a SecureProxy URL and a per-app
virtual key, and claude-agent-sdk was pointed at them. Afterwards, **Security -> Traffic**
shows the brokered call.

Note the reply closes with `Your case number is ...`. Nothing in the application's own code
does that -- it comes from the bundled house rule in `skills/support-reply-style/`,
which AgentBox scanned, approved and mounted read-only, and which the app folds
into its system instructions.

**3. Trigger SecureProxy DLP** -- `POST /process` with an SSN in the text.
Expect **403**. The refusal is the demonstration: the message never reached
the model. **Security -> Audit Log** has the matching event.

**4. MCP tool call** -- `POST /mcp/save-interaction` with `{"customer_id":
"C-1042", "summary": "..."}`. The app has no direct route to any MCP server;
this goes through MCP Bridge, which checks that *this* application holds a
grant for the bundled customer system (`mcp/crm/`) and for that operation,
then records the call. `POST /mcp/find-customer` with `{"query": "Lindqvist"}`
reads the record back; the server declares that operation read-only, so the
bridge never holds it. `POST /mcp/purge-inactive-customers` is the third
operation and the reason there is a third: it is classified **destructive**
by the bridge's own classifier, so the call is held and answered **502** with
the bridge's own sentence and an approval id. Approve or deny it under
**Security**, then send it again -- a held call never resumes by itself.

These are the same three operations the agent itself uses in steps 4 and 5 of
its page; the routes exist so an operator can drive them from the API
console without a model in the loop.

**5. Trigger AFG scan** -- `POST /demo/touch-agent-file`. Writes a file
containing a known prompt-injection string into the agent-files directory.
AFG scans content, not just writes, so a clean file produces nothing;
this one trips a real rule. **Security -> Audit Log**, within a few seconds.

**6. Install a package** -- `POST /demo/install-package`. The control that had
no trigger anywhere in the product until now. Three packages, three outcomes,
all real policy decisions:

| Body | Verdict | Why |
|---|---|---|
| `{"package": "six"}` | `allow` | a tiny compatibility shim on Package Guard's baseline allowlist |
| `{"package": "colourama"}` | `block` | a real typosquat of colorama that shipped a clipboard hijacker |
| `{"package": "docopt"}` | `hold` | dependency-free and on neither list, so policy has to ask you |

A non-zero `exit_code` on the last two means the platform worked. After the
`hold`, go to **Security -> Package Guard approvals**: the request is waiting there
for you to allow or deny. That queue was permanently empty before this endpoint
existed.

If `infra_failure` comes back non-null, Package Guard *failed* rather than decided.
It fails closed, so that state otherwise reads exactly like a policy block.

**7. Try to reach the internet** -- `POST /demo/fetch-url` with
`{"host": "example.com"}`. Expect `status: 403`. This app's only route out is
SecureProxy's forward proxy, and its shipped policy is "block all except listed
destinations" with an empty list.

Now edit this application, add `example.com` to **Allowed destinations**, save,
and call it again: `status: 200`. That before-and-after is the point -- a
policy you never see refuse anything is a policy you have no reason to believe.

**8. Watch the agent work** -- `POST /process/stream`. The same run as step 2,
as server-sent events: `token` as text arrives, `tool` each time the agent calls
something, `tool_result` with the platform's verdict on that call. AgentBox secures *agentic* applications, and this is the agency the
rest of the tour is protecting.

**9. Reset it** -- this app declares `workspace_persistent: true`, so whatever
you installed at step 6 survives a restart (the customer system keeps its own
records in its own container), and the card
offers **Reset workspace** to put it all back.

## The concept

What this agent *is* lives in `concept/`, beside `app/`, and nothing under
`app/` knows which concept it runs:

- `concept/prompt.md` -- the job, one page. `build_system_instructions`
  puts it first, then any standing instructions the environment supplies.
- `concept/concept.json` -- what the app's own page renders and cannot
  derive from a live signal: the name, the eight steps (title, sub, who, the
  literal prompt), the sentence for each outcome on the protected and the
  unprotected side, plain labels for tool calls, and the scorecard lines.
  Served as `GET /concept`. Posture is deliberately *not* in it: the page
  derives that from `GET /runtime-info`, so a concept file can never claim to
  be protected.
- `concept/samples/` -- the fixtures the steps quote (here, six tickets).

Swap the directory and the same kit is a different product. This one is
**Support Desk**: it answers tickets for a small shop, looks customers up,
records what it did, and hands a case over when the house rules say so.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Liveness. AgentBox polls this to mark the app running. |
| POST | `/process` | One message to the agent: `{"input": ..., "question": ...}` (`document` is still accepted). Answers `{answer, backend, tool_calls}`. |
| POST | `/process/stream` | The same, as server-sent events. |
| POST | `/process/upload` | The same, with the message as a UTF-8 text file. |
| POST | `/mcp/find-customer` | Read the bundled customer system through MCP Bridge (declared read-only, never held). |
| POST | `/mcp/save-interaction` | Write a note to a customer's record through the bridge. |
| POST | `/mcp/purge-inactive-customers` | The destructive operation: held by the bridge for a manager, answered 502 with the approval id. |
| POST | `/demo/install-package` | Install a package and report Package Guard's verdict. |
| POST | `/demo/fetch-url` | Attempt egress and report SecureProxy's verdict. |
| POST | `/demo/touch-agent-file` | Write a flagged file, so AFG has something to find. |
| GET | `/runtime-info` | What this app can observe about its own confinement. |
| GET | `/concept` | The concept this app runs: name, steps, the sentence per outcome (`concept/concept.json`). |
| GET | `/` | The browser console. A static mount, not a route -- it is not in the API document. |

The app listens on **8080**, and declares it in `agentbox-config.yaml`. All
seven starters declare different ports so any of them can be installed side by
side.

## What is wired to what

**Model access.** Every model call goes through SecureProxy. The app never
holds a real provider key -- AgentBox injects `KOBIL_SECUREPROXY_URL` and a
virtual key, and the backend binds `ANTHROPIC_API_KEY` to them at startup. Direct
provider fallback is refused rather than silently allowed.

**Package installs.** Runtime `pip` installs are governed by Package Guard,
which resolves the dependency tree, scans the real artifact, checks advisories,
and then allows, holds for approval, or blocks. Step 6 drives this.

**Skills -- the house rules.** `skills/support-reply-style/` and
`skills/escalation-rules/` are the written rules the reply style and the
hand-over wording come from. Each carries a closing line the agent must
produce (`Your case number is ...`, `Handing this to a colleague:`), so a
reply either follows the rule or visibly does not.

To watch the Skill Scanner catch something, add a skill of your own under
**Skills** containing an instruction to exfiltrate a file or to ignore prior
instructions; it is held rather than approved. Nothing risky ships in
`skills/` on purpose -- a starter that arrives with a flagged asset looks
broken rather than instructive. (The page's *own script* in `concept/` does
quote a hostile ticket, because step 3 is about stopping one; that directory
is the app's source, not a skill, and nothing scans it as one.)

**MCP -- the customer system.** `mcp/crm/` is a real MCP server built and run
as its own container, reachable only through MCP Bridge, seeded with forty
invented customers. Its three operations take three different paths:
`find_customer` is declared read-only, `save_interaction` writes, and
`purge_inactive_customers` is classified destructive and therefore held for a
manager's approval. Bundled servers are never auto-bound: enable, validate,
approve the tools, assign the server to this application and rebuild, once
per install.

**Egress.** `network_mode: whitelist` with an empty destination list: the app
reaches the model through SecureProxy and nothing else. Package registries are
deliberately *not* listed -- Package Guard's baseline already carries them, which is
what makes deny-by-default workable.

## The API document

`openapi.json` is what enables **Try API**; the console is gated on that
document parsing, so an app without one gets no console at all.

Regenerate it after changing a route:

```bash
python -c "import json, sys; sys.path.insert(0, '.'); from app.main import app; \\
  print(json.dumps(app.openapi(), indent=2))" > openapi.json
```


`tests/unit/tools/test_starter_app_api_doc.py` fails if it drifts from the
routes in source.
