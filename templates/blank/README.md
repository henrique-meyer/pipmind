# Blank trader starter

`blank-v1` supplies a reusable [identity](identity.md) and [playbook](playbook.md) with no inherited trading strategy or experience. It includes standing operating constraints; “blank” does not remove safety, truthful recordkeeping or autonomous learning. This is a template for a new mind, distinct from cloning an experienced `.pipmind` bundle.

Copy the two files to an ignored directory such as `.data/new-trader/`, then review and customize those private copies. The tracked starter uses LF line endings so its version has stable source bytes across platforms. If you customize it, choose a distinct initial policy version and retain its `blank-v1` origin; the command below uses the unmodified starter. Configure a unique trader ID and destination binding through the [existing private SQL setup](../../docs/shadow.md#private-configuration-and-owner-commands). Against a new, explicitly selected local development database/mind:

```sh
python -m pipmind.shadow init --identity .data/new-trader/identity.md --playbook .data/new-trader/playbook.md --version blank-v1
python -m pipmind.shadow status
```

The existing initializer stores the exact text in SQL, creates a new lineage, empty views/theses/waiting conditions and no historical records, and starts paused. It refuses an already initialized trader. It does not copy credentials, account state, source history or owner permissions. The CLI reads the files from the caller's working directory; keep real copies outside the image and Git. Review existing private environment values before selecting a destination; these commands are not a request to initialize or change the hosted trader.

The files are instructions, not an implementation of chat-edit enforcement or a risk boundary. The [private owner chat and confirmed-edit design](../../docs/architecture.md#private-owner-chat-and-confirmed-edits) still requires its UI, durable SQL edit state and authorization checks. Confirmation applies only to direct edits through chat; nightly review and all other autonomous learning retain their existing rules and never wait for chat consent. Today, edits to a copied file do not update an initialized SQL mind. Do not replace an existing mind with a starter or claim that changing Markdown deployed a new policy. `resume-shadow` permits shadow journaling only; neither initialization nor chat authorizes orders, paid calls, provisioning or live trading.

Future onboarding presents **Start blank**, **Clone experience** and **Recover original trader** as distinct choices. Start blank uses this versioned policy source; experience clone preserves source learning/provenance under a new lineage; recovery requires matching operational state and broker reconciliation. Import/init never silently overwrites an active mind. Pending proposal approvals from another trader are not reusable in the destination.
