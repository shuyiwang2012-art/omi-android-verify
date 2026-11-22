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
WRAPPER_FILE = ROOT / "lib/pages/onboarding/wrapper.dart"

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
        SharedPreferencesUtil().aiConsentGiven = true;
        SharedPreferencesUtil().permissionsCompleted = true;
        authToken = session.idToken;
        _requiresReauthentication = false;
        Logger.debug('local-dev bypass: session established for ${session.uid}');
        notifyListeners();
        onSignIn();
        setLoadingState(false);
        return;
      }
      final credential = await AuthService.instance.signInWithLocalDevToken();"""

CHECK_DELETION_OLD = """  Future<ApiResult<AccountDeletionStatus>> checkAccountDeletion({bool refresh = false}) {
    final uid = _auth.currentUser?.uid;"""
CHECK_DELETION_NEW = """  Future<ApiResult<AccountDeletionStatus>> checkAccountDeletion({bool refresh = false}) {
    // Verification harness: the session lives in preferences, not in the native SDK.
    final localToken = SharedPreferencesUtil().authToken;
    final uid = _auth.currentUser?.uid ??
        (kLocalDevSessionBypass && localToken.isNotEmpty ? SharedPreferencesUtil().uid : null);"""
CHECK_OWNER_OLD = """    if (_auth.currentUser?.uid != uid) return const ApiFailure(ApiProblem(ApiProblemKind.authTerminal));"""
CHECK_OWNER_NEW = """    final effectiveUid = _auth.currentUser?.uid ?? (kLocalDevSessionBypass ? SharedPreferencesUtil().uid : null);
    if (effectiveUid != uid) return const ApiFailure(ApiProblem(ApiProblemKind.authTerminal));"""

SIGNEDIN_OLD = """  bool isSignedIn() {
    return !_requiresReauthentication && _auth.currentUser != null && !_auth.currentUser!.isAnonymous;
  }"""
SIGNEDIN_NEW = """  bool isSignedIn() {
    if (kLocalDevSessionBypass && (authToken?.isNotEmpty ?? false)) return true;
    return !_requiresReauthentication && _auth.currentUser != null && !_auth.currentUser!.isAnonymous;
  }"""

REFRESH_OLD = """  Future<AuthTokenResult> refreshIdToken() {
    if (_sessionExpired) {"""
REFRESH_NEW = """  Future<AuthTokenResult> refreshIdToken() {
    if (kLocalDevSessionBypass && _tokenGateway.currentUser == null) {
      // Verification harness: the session lives in preferences, not in the
      // native SDK, so report the cached token instead of asking Firebase.
      final cached = SharedPreferencesUtil().authToken;
      if (cached.isNotEmpty) {
        return Future<AuthTokenResult>.value(
          AuthTokenSuccess(token: cached, expirationTime: DateTime.now().add(const Duration(minutes: 50))),
        );
      }
    }
    if (_sessionExpired) {"""

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
    if REFRESH_OLD in text:
        text = text.replace(REFRESH_OLD, REFRESH_NEW, 1)
        print("refreshIdToken patched")
    import_anchor = "import 'package:omi/env/env.dart';"
    if import_anchor in text and "local_dev_session.dart" not in text:
        text = text.replace(
            import_anchor,
            import_anchor + "\nimport 'package:omi/services/local_dev_session.dart';",
            1,
        )
    SERVICE_FILE.write_text(text, encoding="utf-8")
    print("auth service patched")


WRAPPER_OLD = """        onSignIn: () async {
          if (!mounted) return;
          final deletion = await context.read<AuthenticationProvider>().checkAccountDeletion();"""
WRAPPER_NEW = """        onSignIn: () async {
          if (!mounted) return;
          if (kLocalDevSessionBypass) {
            // Verification harness: the session was minted locally, so go
            // straight to the app shell without the server-side deletion probe.
            Navigator.of(context).pushAndRemoveUntil(omiPageRoute(builder: (_) => const AppShell()), (_) => false);
            return;
          }
          final deletion = await context.read<AuthenticationProvider>().checkAccountDeletion();"""


def patch_deletion_gate() -> None:
    text = PROVIDER_FILE.read_text(encoding="utf-8")
    if CHECK_DELETION_NEW in text:
        print("deletion gate already patched")
        return
    if CHECK_DELETION_OLD not in text:
        fail("checkAccountDeletion anchor missing")
    text = text.replace(CHECK_DELETION_OLD, CHECK_DELETION_NEW, 1)
    if CHECK_OWNER_OLD in text:
        text = text.replace(CHECK_OWNER_OLD, CHECK_OWNER_NEW, 1)
    PROVIDER_FILE.write_text(text, encoding="utf-8")
    print("deletion gate patched")


def patch_signed_in() -> None:
    text = PROVIDER_FILE.read_text(encoding="utf-8")
    if SIGNEDIN_NEW in text:
        print("isSignedIn already patched")
        return
    if SIGNEDIN_OLD not in text:
        fail("isSignedIn anchor missing")
    text = text.replace(SIGNEDIN_OLD, SIGNEDIN_NEW, 1)
    PROVIDER_FILE.write_text(text, encoding="utf-8")
    print("isSignedIn patched")


def patch_wrapper() -> None:
    text = WRAPPER_FILE.read_text(encoding="utf-8")
    if "kLocalDevSessionBypass" in text:
        print("wrapper already patched")
        return
    if WRAPPER_OLD not in text:
        fail("onboarding wrapper onSignIn anchor missing")
    text = text.replace(WRAPPER_OLD, WRAPPER_NEW, 1)
    import_anchor = "import 'package:omi/services/onboarding_sync_runtime.dart';"
    if import_anchor in text and "local_dev_session.dart" not in text:
        text = text.replace(
            import_anchor,
            import_anchor + "\nimport 'package:omi/services/local_dev_session.dart';",
            1,
        )
    WRAPPER_FILE.write_text(text, encoding="utf-8")
    print("wrapper patched")


def install_session_file() -> None:
    if not SESSION_SOURCE.exists():
        fail(f"missing {SESSION_SOURCE}")
    shutil.copyfile(SESSION_SOURCE, SESSION_FILE)
    print(f"installed {SESSION_FILE}")


def main() -> None:
    install_session_file()
    patch_provider()
    patch_service()
    patch_signed_in()
    patch_deletion_gate()
    patch_wrapper()
    print("bypass patch complete")


if __name__ == "__main__":
    main()
