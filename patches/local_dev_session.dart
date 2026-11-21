import 'dart:convert';

import 'package:http/http.dart' as http;

import 'package:omi/env/env.dart';

/// Build-time switch for the offline verification harness.
///
/// The Firebase Android SDK reaches Google's Firebase Installations service
/// before it will accept a custom token. On a network where that host cannot be
/// reached (for example a proxy that only forwards a domain whitelist) the SDK
/// fails before it ever talks to the local Auth emulator, so `local_dev` builds
/// cannot sign in at all.
///
/// When this flag is compiled in (`--dart-define=OMI_LOCAL_SESSION_BYPASS=true`)
/// the app asks the harness backend for a custom token and exchanges it for an
/// emulator ID token over plain HTTP, keeping that session locally instead of
/// calling the native SDK. Production profiles never reach this code: the flag
/// defaults to off and every call site also checks `Env.profile`.
const bool kLocalDevSessionBypass = bool.fromEnvironment('OMI_LOCAL_SESSION_BYPASS');

class LocalDevSession {
  const LocalDevSession({required this.uid, required this.idToken});

  final String uid;
  final String idToken;
}

/// Mints a session end to end: backend custom token -> Auth emulator ID token.
///
/// Both hops stay on the local machine: the backend runs next to the emulator
/// and the emulator is reached through `adb reverse tcp:9099`.
Future<LocalDevSession> mintLocalDevSession({String uid = 'local-dev-user'}) async {
  final tokenResponse = await http.post(
    Uri.parse('${Env.authApiBaseUrl}v1/auth/local-dev/custom-token'),
    headers: {'Content-Type': 'application/x-www-form-urlencoded'},
    body: {'uid': uid},
  ).timeout(const Duration(seconds: 10));

  if (tokenResponse.statusCode != 200) {
    throw StateError('local-dev token endpoint returned HTTP ${tokenResponse.statusCode}');
  }

  final customToken = (json.decode(tokenResponse.body) as Map<String, dynamic>)['custom_token'] as String?;
  if (customToken == null || customToken.isEmpty) {
    throw StateError('local-dev token endpoint returned no custom token');
  }

  final exchangeUri = Uri.parse(
    'http://${Env.firebaseAuthEmulatorHost}:${Env.firebaseAuthEmulatorPort}'
    '/identitytoolkit.googleapis.com/v1/accounts:signInWithCustomToken?key=demo-omi-local',
  );

  final exchange = await http.post(
    exchangeUri,
    headers: {'Content-Type': 'application/json'},
    body: json.encode({'token': customToken, 'returnSecureToken': true}),
  ).timeout(const Duration(seconds: 10));

  if (exchange.statusCode != 200) {
    throw StateError('Auth emulator exchange returned HTTP ${exchange.statusCode}: ${exchange.body}');
  }

  final decoded = json.decode(exchange.body) as Map<String, dynamic>;
  final idToken = decoded['idToken'] as String?;
  if (idToken == null || idToken.isEmpty) {
    throw StateError('Auth emulator exchange returned no ID token');
  }
  // The emulator's VerifyCustomTokenResponse omits localId, so the principal we
  // asked the harness to mint is authoritative here.
  final localId = (decoded['localId'] as String?) ?? uid;

  return LocalDevSession(uid: localId, idToken: idToken);
}
