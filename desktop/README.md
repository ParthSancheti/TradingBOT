# Jarvis Trade desktop

Run `Start Jarvis.cmd` from the project root. This starts the Electron desktop,
which owns a Python worker over private process pipes. It reuses the existing
OS-vault Sheets connection and paired Chrome native helper. It does not require
the development web server or an admin-authentication bypass.

In Workspace setup, enter the account's WhatsApp number and stock sender numbers.
Sender names are read from WhatsApp. Start opens Chrome and enables the extension's
rolling discovery; Stop disables new observation batches. Install/reload the
updated extension once after updating this source build. Chrome extension
installation still requires a user action.

The renderer has no Node access. Only explicit operations cross the preload IPC
boundary. External navigation, popups, downloads, and permissions are denied.
Provider and signing keys are stored in the OS vault, not in renderer storage.

## Current capability boundaries

This is one application implementation, without a separate test GUI. However it
is not a validated production release. Live WhatsApp DOM coverage remains partial:
the extension cycles through rendered inbox rows, captures verified incoming text,
and preserves stable phone/LID/group identities. It cannot promise every message,
archived chat, attachment, or historical event. Source labels never establish
identity. Classification and the workspace assistant use a user-provided Gemini
connection (one to five OS-vault keys) and model entitlement. Same-project keys
share quota; quota errors pause the whole pool. Existing xAI credentials are not
used by the desktop. Model validation is still required with real credentials.
Messages without a model are retained for review. Automatic sending, quotes,
payments and autonomous trade execution remain disabled/unimplemented.

`npm test` checks IPC and asset boundaries. Backend and extension tests live at
the project root. `npm run package` builds the bundled Python service and Electron
application on the target operating system. Distribution still needs code signing,
native-helper/extension installation integration, macOS validation and live soak
testing. Existing native-helper pairing is reused on this development machine.
