"""Apply the local_dev session bypass to the Omi app checkout.

The harness cannot reach Google's Firebase Installations service from this
network, so the native FirebaseAuth sign-in never leaves the device. This patch
lets a `local_dev` build mint its session through the local Auth emulator REST
endpoint instead, behind the compile-time flag `OMI_LOCAL_SESSION_BYPASS`.

Only three files are touched, none in production code paths:
  * lib/services/local_dev_session.dart  (new)
  * lib/providers/auth_provider.dart     (one guarded branch)
  * lib/services/auth_service.dart       (one guarded early return)

Run from the repository root (the directory that contains `app/`).
"""

from pathlib import Path
import shutil
import sys

ROOT = Path("app")
SESSION_FILE = ROOT / "lib/services/local_dev_session.dart"
PROVIDER_FILE = ROOT / "lib/providers/auth_provider.dart"
SERVICE_FILE = ROOT / "lib/services/auth_service.dart"

SESSION_SOURCE = Path(__file__).with_name("local_dev_session.dart")

PROVIDER_IMPORT_ANCHOR = "import 'package:omi/services/auth_service.dart';"
PROVIDER_BRANCH_OLD = """    setLoadingState(true);
    try {
      final credential = await AuthService.instance.signInWithLocalDevToken();"""
PROVIDER_BRANCH_NEW = """    setLoadingState(true);
    try {
      if (kLocalDevSessionBypass) {
        // Verification harness only: the native Firebase SDK cannot reach
        // Firebase Installations on this network, so the session is minted
        // through the emulator REST endpoint and kept locally.
        final session = await mintLocalDevSession();
        SharedPreferencesUtil().uid = session.uid;
        SharedPreferencesUtil().authToken = session.idToken;
        SharedPreferencesUtil().onboardingCompleted = true;
        authToken = session.idToken;
        _requiresReauthentication = false;
        Logger.debug('local-dev bypass: session established for ${session.uid}');
        notifyListeners();
        onSignIn();
        setLoadingState(false);
        return;
      }
      final credential = await AuthService.instance.signInWithLocalDevToken();"""

SERVICE_OLD = """  Future<String?> getIdToken() async {
    final result = await refreshIdToken();"""
SERVICE_NEW = """  Future<String?> getIdToken() async {
    if (kLocalDevSessionBypass && FirebaseAuth.instance.currentUser == null) {
      final cached = SharedPreferencesUtil().authToken;
      if (cached != null && cached.isNotEmpty) return cached;
    }
    final result = await refreshIdToken();"""


def fail(message: str) -> None:
    print(f"PATCH FAILED: {message}")
    sys.exit(1)


def patch_provider() -> None:
    text = PROVIDER_FILE.read_text(encoding="utf-8")
    if "kLocalDevSessionBypass" in text:
        print("provider already patched")
        return
    if PROVIDER_IMPORT_ANCHOR not in text:
        fail("provider import anchor missing")
    text = text.replace(
        PROVIDER_IMPORT_ANCHOR,
        PROVIDER_IMPORT_ANCHOR + "\nimport 'package:omi/services/local_dev_session.dart';",
        1,
    )
    if PROVIDER_BRANCH_OLD not in text:
        fail("provider sign-in branch anchor missing")
    text = text.replace(PROVIDER_BRANCH_OLD, PROVIDER_BRANCH_NEW, 1)
    PROVIDER_FILE.write_text(text, encoding="utf-8")
    print("provider patched")


def patch_service() -> None:
    text = SERVICE_FILE.read_text(encoding="utf-8")
    if "kLocalDevSessionBypass" in text:
        print("auth service already patched")
        return
    if SERVICE_OLD not in text:
        fail("auth service getIdToken anchor missing")
    text = text.replace(SERVICE_OLD, SERVICE_NEW, 1)
    import_anchor = "import 'package:omi/env/env.dart';"
    if import_anchor in text and "local_dev_session.dart" not in text:
        text = text.replace(
            import_anchor,
            import_anchor + "\nimport 'package:omi/services/local_dev_session.dart';",
            1,
        )
    SERVICE_FILE.write_text(text, encoding="utf-8")
    print("auth service patched")


def install_session_file() -> None:
    if not SESSION_SOURCE.exists():
        fail(f"missing {SESSION_SOURCE}")
    shutil.copyfile(SESSION_SOURCE, SESSION_FILE)
    print(f"installed {SESSION_FILE}")


def main() -> None:
    install_session_file()
    patch_provider()
    patch_service()
    print("bypass patch complete")


if __name__ == "__main__":
    main()
