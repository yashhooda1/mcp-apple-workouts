//  HoodaRunSiriIntents.swift — add to the iOS HoodaRun app target.
//
//  Siri AI and Shortcuts use App Intents, NOT MCP natively.
//  The app reads iCloud/CloudKit for the next workout and uses the already
//  configured encrypted-in-Keychain HTTP token for private server actions.
//  Never embed an MCP bearer token in a Shortcuts parameter or Swift source.
//
//  Requires: AppIntents, iOS 18+ target, existing MCPPlanSync.swift,
//  MCPSyncSettings, HoodaRun remote server HTTPS configuration.
//  An iPhone companion target is required; Apple Watch execution must
//  be tested with an installed, paired Watch app / synchronized Shortcut.

import AppIntents
import Foundation

private enum HoodaRunSiriError: LocalizedError {
    case noRemoteServer
    case invalidResponse
    case rejected(String)

    var errorDescription: String? {
        switch self {
        case .noRemoteServer:
            return "Set a secure HTTPS remote MCP server in HoodaRun Settings " +
                "before using voice actions that publish workouts or search races."
        case .invalidResponse:
            return "HoodaRun received an invalid response from the workout server."
        case .rejected(let message):
            return message
        }
    }
}

private enum HoodaRunSiriClient {
    static func isoDate(_ value: Date) -> String {
        let formatter = DateFormatter()
        formatter.calendar = Calendar(identifier: .gregorian)
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.timeZone = .current
        formatter.dateFormat = "yyyy-MM-dd"
        return formatter.string(from: value)
    }

    /// All requests are deterministic, allow-listed actions at POST /api/siri.
    /// No generic remote MCP tool executor is exposed to the voice system.
    static func call(_ body: [String: Any]) async throws -> [String: Any] {
        guard let source = MCPSyncSettings.source(),
              case .http(let base, let token) = source,
              base.scheme?.lowercased() == "https",
              !token.isEmpty else {
            throw HoodaRunSiriError.noRemoteServer
        }
        var request = URLRequest(url: base.appendingPathComponent("api/siri"))
        request.httpMethod = "POST"
        request.timeoutInterval = 15
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.cachePolicy = .reloadIgnoringLocalCacheData
        request.httpBody = try JSONSerialization.data(withJSONObject: body)

        let (data, response) = try await URLSession.shared.data(for: request)
        guard let result = try JSONSerialization.jsonObject(with: data) as? [String: Any]
        else { throw HoodaRunSiriError.invalidResponse }
        let code = (response as? HTTPURLResponse)?.statusCode ?? 0
        guard code == 200 else {
            throw HoodaRunSiriError.rejected(
                result["error"] as? String ?? "HoodaRun HTTP error \(code)"
            )
        }
        return result
    }

    static func prepareAndPublish(options: [String: Any],
                                  confirm: () async throws -> Void) async throws -> String {
        let preview = try await call(["action": "preview_run", "options": options])
        guard let token = preview["preview_token"] as? String else {
            throw HoodaRunSiriError.invalidResponse
        }
        // The caller MUST request system confirmation before this callback returns.
        try await confirm()
        let published = try await call([
            "action": "publish_run", "options": options, "preview_token": token,
        ])
        return published["message"] as? String ?? "Workout added to the feed."
    }
}

struct HoodaRunNextWorkoutIntent: AppIntent {
    static var title: LocalizedStringResource = "Next HoodaRun Workout"
    static var description = IntentDescription(
        "Read the next planned workout from your iPhone's HoodaRun feed or iCloud."
    )

    func perform() async throws -> some IntentResult & ProvidesDialog {
        let message = try await MCPPlanSync.shared.nextPlannedWorkoutSummary()
        return .result(dialog: IntentDialog(stringLiteral: message))
    }
}

struct HoodaRunSyncWatchIntent: AppIntent {
    static var title: LocalizedStringResource = "Sync HoodaRun to Apple Watch"
    static var description = IntentDescription(
        "Ask the iPhone companion to schedule your published workouts with WorkoutKit."
    )

    func perform() async throws -> some IntentResult & ProvidesDialog {
        let result = try await MCPPlanSync.shared.sync()
        let message = "HoodaRun sync finished. \(result.scheduled) workouts scheduled, " +
            "\(result.pending) pending and \(result.errors.count) errors."
        return .result(dialog: IntentDialog(stringLiteral: message))
    }
}

struct HoodaRunAddEasyRunIntent: AppIntent {
    static var title: LocalizedStringResource = "Add HoodaRun Easy Run"
    static var description = IntentDescription(
        "Preview and confirm an easy run in the HoodaRun feed for Apple Watch."
    )

    @Parameter(title: "Distance in miles")
    var miles: Double

    @Parameter(title: "Run date")
    var runDate: Date?

    static var parameterSummary: some ParameterSummary {
        Summary("Schedule a \(\.$miles)-mile easy run in HoodaRun")
    }

    func perform() async throws -> some IntentResult & ProvidesDialog {
        let day = HoodaRunSiriClient.isoDate(runDate ?? Date())
        let options: [String: Any] = [
            "kind": "easy", "date": day, "miles": miles,
        ]
        let message = try await HoodaRunSiriClient.prepareAndPublish(options: options) {
            try await requestConfirmation()
        }
        return .result(dialog: IntentDialog(stringLiteral: message))
    }
}

struct HoodaRunAddIntervalsIntent: AppIntent {
    static var title: LocalizedStringResource = "Add HoodaRun Intervals"
    static var description = IntentDescription(
        "Build a repeated running workout, with standing or jogging recoveries."
    )

    @Parameter(title: "Number of repeats")
    var repeats: Int

    @Parameter(title: "Distance per repeat in meters")
    var meters: Int

    @Parameter(title: "Fast pace per mile, for example 5:40")
    var fastPace: String

    @Parameter(title: "Slow pace per mile, for example 5:55")
    var slowPace: String

    @Parameter(title: "Recovery seconds")
    var recoverySeconds: Int

    @Parameter(title: "Standing rest instead of jogging")
    var standingRest: Bool

    @Parameter(title: "Workout date")
    var runDate: Date?

    static var parameterSummary: some ParameterSummary {
        Summary("Schedule \(\.$repeats) repeats of \(\.$meters) meters in HoodaRun")
    }

    func perform() async throws -> some IntentResult & ProvidesDialog {
        let options: [String: Any] = [
            "kind": "intervals",
            "date": HoodaRunSiriClient.isoDate(runDate ?? Date()),
            "repeats": repeats, "work_meters": meters,
            "pace_fast": fastPace, "pace_slow": slowPace,
            "recovery_seconds": recoverySeconds,
            "recovery_style": standingRest ? "standing" : "jog",
        ]
        let message = try await HoodaRunSiriClient.prepareAndPublish(options: options) {
            try await requestConfirmation()
        }
        return .result(dialog: IntentDialog(stringLiteral: message))
    }
}

enum HoodaRunTexasRegion: String, AppEnum {
    case austin, dallas, houston

    static var typeDisplayRepresentation = TypeDisplayRepresentation(name: "Texas Region")
    static var caseDisplayRepresentations: [Self: DisplayRepresentation] = [
        .austin: "Austin", .dallas: "Dallas / Fort Worth", .houston: "Houston",
    ]
}

struct HoodaRunTexasRacesIntent: AppIntent {
    static var title: LocalizedStringResource = "Find Texas Races in HoodaRun"
    static var description = IntentDescription(
        "Read upcoming races near Austin, Dallas/Fort Worth, or Houston."
    )

    @Parameter(title: "Region")
    var region: HoodaRunTexasRegion

    func perform() async throws -> some IntentResult & ProvidesDialog {
        let response = try await HoodaRunSiriClient.call([
            "action": "texas_races", "area": region.rawValue.capitalized,
        ])
        let answer = response["message"] as? String ?? "No race results found."
        return .result(dialog: IntentDialog(stringLiteral: answer))
    }
}

enum HoodaRunDiet: String, AppEnum {
    case vegan
    case lactoVegetarian = "lacto_vegetarian"
    case vegetarian

    static var typeDisplayRepresentation = TypeDisplayRepresentation(name: "Runner Diet")
    static var caseDisplayRepresentations: [Self: DisplayRepresentation] = [
        .vegan: "Vegan", .lactoVegetarian: "Lacto-Vegetarian",
        .vegetarian: "Vegetarian",
    ]
}

struct HoodaRunMealIdeasIntent: AppIntent {
    static var title: LocalizedStringResource = "HoodaRun Meal Ideas"
    static var description = IntentDescription(
        "Get simple plant-forward endurance fueling meal suggestions."
    )

    @Parameter(title: "Diet")
    var diet: HoodaRunDiet

    func perform() async throws -> some IntentResult & ProvidesDialog {
        let response = try await HoodaRunSiriClient.call([
            "action": "meal_ideas", "diet": diet.rawValue,
        ])
        let answer = response["message"] as? String ?? "No meal suggestions available."
        return .result(dialog: IntentDialog(stringLiteral: answer))
    }
}

/// Compile this into the iPhone app target. Siri/Shortcuts discover it
/// automatically when the app is installed; phrases must include app name.
struct HoodaRunSiriShortcuts: AppShortcutsProvider {
    static var appShortcuts: [AppShortcut] {
        AppShortcut(
            intent: HoodaRunNextWorkoutIntent(),
            phrases: [
                "What's my next workout in \(.applicationName)",
                "Show my next \(.applicationName) workout",
            ],
            shortTitle: "Next Workout",
            systemImageName: "figure.run"
        )
        AppShortcut(
            intent: HoodaRunSyncWatchIntent(),
            phrases: [
                "Sync my \(.applicationName) workouts",
                "Sync \(.applicationName) to Apple Watch",
            ],
            shortTitle: "Sync Watch",
            systemImageName: "applewatch"
        )
        AppShortcut(
            intent: HoodaRunAddEasyRunIntent(),
            phrases: ["Schedule an easy run in \(.applicationName)"],
            shortTitle: "Add Easy Run",
            systemImageName: "figure.run.circle"
        )
        AppShortcut(
            intent: HoodaRunAddIntervalsIntent(),
            phrases: ["Add interval training in \(.applicationName)"],
            shortTitle: "Add Intervals",
            systemImageName: "timer"
        )
        AppShortcut(
            intent: HoodaRunTexasRacesIntent(),
            phrases: ["Find Texas races in \(.applicationName)"],
            shortTitle: "Texas Races",
            systemImageName: "flag.checkered"
        )
        AppShortcut(
            intent: HoodaRunMealIdeasIntent(),
            phrases: ["Get meal ideas from \(.applicationName)"],
            shortTitle: "Runner Meals",
            systemImageName: "fork.knife"
        )
    }
}
