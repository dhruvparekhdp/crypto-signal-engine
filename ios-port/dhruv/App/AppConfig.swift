import Foundation

/// Where the backend lives, and the Keychain key the API token is stored
/// under. Not a settings struct read from a plist on purpose — there is
/// exactly one server this app talks to, and hiding that behind a config
/// layer before there is a second server to switch between would be
/// building for a requirement that does not exist yet.
enum AppConfig {
    /// The deployed backend. Change this one line if the server moves;
    /// nothing else in the app should ever hardcode it.
    ///
    /// Plain http, because the box serves the dashboard on port 8080 with no
    /// certificate — which means the bearer token crosses the network in
    /// clear text and App Transport Security has to be relaxed to allow it at
    /// all. Acceptable for one operator on their own phone, not acceptable
    /// for anything wider: put this behind a TLS terminator with a real
    /// hostname before the app goes anywhere near TestFlight.
    static let baseURL = URL(string: "http://52.62.37.4:8080")!

    /// Keychain account name for the API bearer token (see KeychainStore).
    static let apiTokenKeychainKey = "api_auth_token"
}
