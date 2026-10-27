//  MCPPlanSync.swift — HoodaRun
//
//  Pulls the apple-workouts-mcp feed (workouts.json), turns each item into a
//  WorkoutKit CustomWorkout, and keeps WorkoutScheduler in step with it:
//  new or changed items are scheduled, items that left the feed are removed,
//  and status.json is written back so the MCP server can report what really
//  landed on the Watch.
//
//  Only workouts this file scheduled are ever removed — anything built in
//  HoodaRun's own workout builder is left alone.
//
//  WorkoutKit caps how many workouts can be scheduled at once
//  (WorkoutScheduler.maxAllowedScheduledWorkoutCount), so the sync fills the
//  free slots with the soonest upcoming items and leaves the rest "pending";
//  each later sync rolls the window forward.
//
//  Requirements: iOS 18+ / watchOS 11+ (step display names), WorkoutKit, and
//  the HealthKit capability already used by HoodaRun.

import CloudKit
import Foundation
import HealthKit
import WorkoutKit

// MARK: - Feed JSON (hooda.workouts.feed/v1)

struct MCPFeed: Codable {
    let schema: String
    let generated_at: String?
    let workouts: [MCPFeedWorkout]
}

struct MCPFeedWorkout: Codable {
    let id: String
    let rev: String
    let date: String          // YYYY-MM-DD
    let time: String?         // HH:MM, 24h, optional
    let name: String
    let sport: String         // running | strength
    let plan: String?
    let description: String?
    let workout: MCPCustomWorkout
}

struct MCPCustomWorkout: Codable {
    let schema: String
    let activity: String
    let fallback_activity: String?
    let location: String
    let display_name: String
    let warmup: MCPStep?
    let blocks: [MCPBlock]
    let cooldown: MCPStep?
}

struct MCPBlock: Codable {
    let iterations: Int
    let steps: [MCPInterval]
}

struct MCPInterval: Codable {
    let purpose: String       // work | recovery
    let step: MCPStep
}

struct MCPStep: Codable {
    let goal: MCPGoal
    let alert: MCPAlert?
    let display_name: String?
}

struct MCPGoal: Codable {
    let type: String          // time | distance | open
    let seconds: Double?
    let meters: Double?
}

struct MCPAlert: Codable {
    let type: String          // hr_zone | speed
    let zone: Int?
    let min_mps: Double?
    let max_mps: Double?
}

// MARK: - Status JSON (written back for the server)

struct MCPSyncStatus: Codable {
    struct Entry: Codable { let id: String; let rev: String; let date: String }
    struct Pending: Codable { let id: String; let date: String; let reason: String }
    struct Failure: Codable { let id: String; let message: String }

    let app: String
    let synced_at: String
    let max_scheduled: Int
    let scheduled: [Entry]
    let pending: [Pending]
    let errors: [Failure]
}

enum MCPSyncError: LocalizedError {
    case notAuthorized
    case noSource
    case badSchema(String)
    case unsupported(String)
    case http(Int)

    var errorDescription: String? {
        switch self {
        case .notAuthorized: return "HoodaRun isn't allowed to schedule workouts. Allow it in Settings."
        case .noSource: return "Turn on iCloud workouts, pick the MCP feed folder, or set a feed URL."
        case .badSchema(let s): return "Unexpected feed schema: \(s)"
        case .unsupported(let s): return s
        case .http(let code): return "Feed request failed (HTTP \(code))."
        }
    }
}

// MARK: - JSON -> WorkoutKit

enum MCPWorkoutBuilder {
    static func goal(_ g: MCPGoal) -> WorkoutGoal {
        switch g.type {
        case "time":     return .time(g.seconds ?? 0, .seconds)
        case "distance": return .distance(g.meters ?? 0, .meters)
        default:         return .open
        }
    }

    static func alert(_ a: MCPAlert?) -> (any WorkoutAlert)? {
        guard let a else { return nil }
        switch a.type {
        case "hr_zone":
            guard let zone = a.zone else { return nil }
            return HeartRateZoneAlert(zone: zone)
        case "speed":
            guard let lo = a.min_mps, let hi = a.max_mps, lo < hi else { return nil }
            let range = Measurement(value: lo, unit: UnitSpeed.metersPerSecond)
                ... Measurement(value: hi, unit: UnitSpeed.metersPerSecond)
            return SpeedRangeAlert(target: range, metric: .current)
        default:
            return nil
        }
    }

    static func step(_ s: MCPStep) -> WorkoutStep {
        WorkoutStep(goal: goal(s.goal), alert: alert(s.alert), displayName: s.display_name)
    }

    static func activity(_ name: String) -> HKWorkoutActivityType? {
        switch name {
        case "running": return .running
        case "traditional_strength_training": return .traditionalStrengthTraining
        case "functional_strength_training": return .functionalStrengthTraining
        case "walking": return .walking
        case "hiking": return .hiking
        case "cycling": return .cycling
        default: return nil
        }
    }

    static func location(_ name: String) -> HKWorkoutSessionLocationType {
        name == "indoor" ? .indoor : .outdoor
    }

    static func customWorkout(_ w: MCPCustomWorkout) throws -> CustomWorkout {
        let candidates = [w.activity, w.fallback_activity].compactMap { $0 }
        guard let activity = candidates.compactMap(activity).first(where: {
            CustomWorkout.supportsActivity($0)
        }) else {
            throw MCPSyncError.unsupported("\(w.display_name): no supported activity in \(candidates)")
        }
        let blocks = w.blocks.map { b in
            IntervalBlock(
                steps: b.steps.map { IntervalStep($0.purpose == "recovery" ? .recovery : .work,
                                                  step: step($0.step)) },
                iterations: max(1, b.iterations)
            )
        }
        return CustomWorkout(
            activity: activity,
            location: location(w.location),
            displayName: w.display_name,
            warmup: w.warmup.map(step),
            blocks: blocks,
            cooldown: w.cooldown.map(step)
        )
    }

    static func dateComponents(_ item: MCPFeedWorkout) -> DateComponents? {
        let d = item.date.split(separator: "-").compactMap { Int($0) }
        guard d.count == 3 else { return nil }
        let t = (item.time ?? "06:00").split(separator: ":").compactMap { Int($0) }
        var c = DateComponents()
        c.calendar = Calendar(identifier: .gregorian)
        c.timeZone = .current
        c.year = d[0]; c.month = d[1]; c.day = d[2]
        c.hour = t.first ?? 6; c.minute = t.count > 1 ? t[1] : 0
        return c
    }
}

// MARK: - Feed sources

enum MCPFeedSource {
    /// A folder (usually iCloud Drive/HoodaRun/MCP) picked once by the user.
    case folder(URL)
    /// The server's HTTP transport: GET /feed.json, POST /status.
    case http(base: URL, token: String)
}

private extension MCPFeedSource {
    func readFeed() async throws -> Data {
        switch self {
        case .folder(let dir):
            return try Self.coordinatedRead(dir.appendingPathComponent("workouts.json"), in: dir)
        case .http(let base, let token):
            var req = URLRequest(url: base.appendingPathComponent("feed.json"))
            req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
            req.cachePolicy = .reloadIgnoringLocalCacheData
            let (data, resp) = try await URLSession.shared.data(for: req)
            let code = (resp as? HTTPURLResponse)?.statusCode ?? 0
            guard code == 200 else { throw MCPSyncError.http(code) }
            return data
        }
    }

    func writeStatus(_ data: Data) async throws {
        switch self {
        case .folder(let dir):
            try Self.coordinatedWrite(data, to: dir.appendingPathComponent("status.json"), in: dir)
        case .http(let base, let token):
            var req = URLRequest(url: base.appendingPathComponent("status"))
            req.httpMethod = "POST"
            req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            req.httpBody = data
            let (_, resp) = try await URLSession.shared.data(for: req)
            let code = (resp as? HTTPURLResponse)?.statusCode ?? 0
            guard code == 200 else { throw MCPSyncError.http(code) }
        }
    }

    // NSFileCoordinator makes iCloud download the file if it's only a placeholder.
    static func coordinatedRead(_ url: URL, in dir: URL) throws -> Data {
        let scoped = dir.startAccessingSecurityScopedResource()
        defer { if scoped { dir.stopAccessingSecurityScopedResource() } }
        var coordError: NSError?
        var result: Result<Data, Error> = .failure(CocoaError(.fileNoSuchFile))
        NSFileCoordinator().coordinate(readingItemAt: url, options: [], error: &coordError) { u in
            result = Result { try Data(contentsOf: u) }
        }
        if let coordError { throw coordError }
        return try result.get()
    }

    static func coordinatedWrite(_ data: Data, to url: URL, in dir: URL) throws {
        let scoped = dir.startAccessingSecurityScopedResource()
        defer { if scoped { dir.stopAccessingSecurityScopedResource() } }
        var coordError: NSError?
        var writeError: Error?
        NSFileCoordinator().coordinate(writingItemAt: url, options: .forReplacing,
                                       error: &coordError) { u in
            do { try data.write(to: u, options: .atomic) } catch { writeError = error }
        }
        if let coordError { throw coordError }
        if let writeError { throw writeError }
    }
}

// MARK: - Settings persistence

enum MCPSyncSettings {
    private static let bookmarkKey = "mcp.feed.folderBookmark"
    private static let urlKey = "mcp.feed.httpURL"
    private static let ownedKey = "mcp.feed.owned"      // [id: rev] scheduled by this sync
    static let tokenAccount = "mcp.feed.token"

    static func saveFolder(_ url: URL) throws {
        let scoped = url.startAccessingSecurityScopedResource()
        defer { if scoped { url.stopAccessingSecurityScopedResource() } }
        let data = try url.bookmarkData(options: [], includingResourceValuesForKeys: nil,
                                        relativeTo: nil)
        UserDefaults.standard.set(data, forKey: bookmarkKey)
        UserDefaults.standard.removeObject(forKey: urlKey)
    }

    static func saveHTTP(_ url: URL, token: String) {
        UserDefaults.standard.set(url.absoluteString, forKey: urlKey)
        UserDefaults.standard.removeObject(forKey: bookmarkKey)
        MCPKeychain.set(token, account: tokenAccount)
    }

    static func source() -> MCPFeedSource? {
        if let data = UserDefaults.standard.data(forKey: bookmarkKey) {
            var stale = false
            if let url = try? URL(resolvingBookmarkData: data, options: [],
                                  relativeTo: nil, bookmarkDataIsStale: &stale) {
                if stale { try? saveFolder(url) }
                return .folder(url)
            }
        }
        if let s = UserDefaults.standard.string(forKey: urlKey), let url = URL(string: s),
           let token = MCPKeychain.get(account: tokenAccount) {
            return .http(base: url, token: token)
        }
        return nil
    }

    static var owned: [String: String] {
        get { UserDefaults.standard.dictionary(forKey: ownedKey) as? [String: String] ?? [:] }
        set { UserDefaults.standard.set(newValue, forKey: ownedKey) }
    }
}

enum MCPKeychain {
    static func set(_ value: String, account: String) {
        let base: [String: Any] = [kSecClass as String: kSecClassGenericPassword,
                                   kSecAttrAccount as String: account]
        SecItemDelete(base as CFDictionary)
        var add = base
        add[kSecValueData as String] = Data(value.utf8)
        add[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlock
        SecItemAdd(add as CFDictionary, nil)
    }

    static func get(account: String) -> String? {
        let q: [String: Any] = [kSecClass as String: kSecClassGenericPassword,
                                kSecAttrAccount as String: account,
                                kSecReturnData as String: true,
                                kSecMatchLimit as String: kSecMatchLimitOne]
        var out: AnyObject?
        guard SecItemCopyMatching(q as CFDictionary, &out) == errSecSuccess,
              let data = out as? Data else { return nil }
        return String(data: data, encoding: .utf8)
    }
}

// MARK: - The sync

actor MCPPlanSync {
    static let shared = MCPPlanSync()

    struct Outcome {
        let scheduled: Int
        let removed: Int
        let pending: Int
        let errors: [String]
    }

    /// Read the same feed/CloudKit sources as the WorkoutKit sync, without
    /// writing or requesting WorkoutKit authorization. Safe for a Siri query.
    func nextPlannedWorkoutSummary() async throws -> String {
        let day = Self.isoDay(Date())
        var incoming: [MCPFeedWorkout] = []
        if let source = MCPSyncSettings.source() {
            let feed = try JSONDecoder().decode(MCPFeed.self, from: try await source.readFeed())
            guard feed.schema == "hooda.workouts.feed/v1" else {
                throw MCPSyncError.badSchema(feed.schema)
            }
            incoming += feed.workouts
        }
        if CloudKitPlanStore.isEnabled, await CloudKitPlanStore.accountAvailable() {
            let cloud = try await CloudKitPlanStore.upcoming(from: day)
            incoming += cloud.map { $0.item }
        }
        guard let next = incoming.filter({ $0.date >= day })
            .sorted(by: { ($0.date, $0.time ?? "") < ($1.date, $1.time ?? "") }).first
        else {
            return "No upcoming workouts yet. Add a plan in HoodaRun."
        }
        // "Planned" is intentionally distinct from "confirmed on the Watch".
        // The WorkoutKit scheduler is not queried as part of this read intent.
        return "Your next planned workout is \(next.name) on \(next.date). " +
            "Open HoodaRun to check whether it is synced to Apple Watch."
    }

    private var running = false

    /// Safe to call often (app launch, scenePhase .active, a "Sync now" button).
    @discardableResult
    func sync() async throws -> Outcome {
        guard !running else { return Outcome(scheduled: 0, removed: 0, pending: 0, errors: []) }
        running = true
        defer { running = false }

        // Two inputs, one schedule: the MCP feed (iCloud Drive or HTTP) and
        // CloudKit records saved from the yashhooda.ai builder.
        let source = MCPSyncSettings.source()
        let useCloudKit = CloudKitPlanStore.isEnabled
        guard source != nil || useCloudKit else { throw MCPSyncError.noSource }

        let scheduler = WorkoutScheduler.shared
        // `try await` throughout: WorkoutKit's scheduler calls have been both
        // throwing and non-throwing across SDKs; this compiles against either.
        if await scheduler.authorizationState != .authorized {
            let state = try await scheduler.requestAuthorization()
            guard state == .authorized else { throw MCPSyncError.notAuthorized }
        }

        let today = Self.isoDay(Date())
        var incoming: [MCPFeedWorkout] = []
        if let source {
            let feed = try JSONDecoder().decode(MCPFeed.self, from: try await source.readFeed())
            guard feed.schema == "hooda.workouts.feed/v1" else { throw MCPSyncError.badSchema(feed.schema) }
            incoming += feed.workouts
        }
        var cloudPairs: [(item: MCPFeedWorkout, record: CKRecord)] = []
        if useCloudKit, await CloudKitPlanStore.accountAvailable() {
            cloudPairs = try await CloudKitPlanStore.upcoming(from: today)
            incoming += cloudPairs.map { $0.item }
        }

        let upcoming = incoming.filter { $0.date >= today }
            .sorted { ($0.date, $0.time ?? "") < ($1.date, $1.time ?? "") }
        let wanted = Dictionary(upcoming.map { ($0.id, $0) }, uniquingKeysWith: { a, _ in a })

        var owned = MCPSyncSettings.owned
        var errors: [MCPSyncStatus.Failure] = []
        var removed = 0

        // 1. Remove what we scheduled that left the feed, changed, or is now past.
        for sched in try await scheduler.scheduledWorkouts {
            let id = sched.plan.id.uuidString
            guard let rev = owned[id] else { continue }          // not ours: never touch
            if let item = wanted[id], item.rev == rev { continue }
            try await scheduler.remove(sched.plan, at: sched.date)
            owned[id] = nil
            removed += 1
        }
        // Forget ids WorkoutKit already dropped (completed or deleted on the Watch).
        let stillScheduled = try await scheduler.scheduledWorkouts
        let live = Set(stillScheduled.map { $0.plan.id.uuidString })
        owned = owned.filter { live.contains($0.key) }

        // 2. Fill free slots with the soonest items not yet on the Watch.
        let cap = WorkoutScheduler.maxAllowedScheduledWorkoutCount
        var free = cap - live.count
        var scheduledCount = 0
        var pending: [MCPSyncStatus.Pending] = []
        for item in upcoming where owned[item.id] != item.rev {
            guard free > 0 else {
                pending.append(.init(id: item.id, date: item.date,
                                     reason: "WorkoutKit limit of \(cap) scheduled workouts"))
                continue
            }
            do {
                guard let uuid = UUID(uuidString: item.id),
                      let when = MCPWorkoutBuilder.dateComponents(item) else {
                    throw MCPSyncError.unsupported("bad id or date")
                }
                let custom = try MCPWorkoutBuilder.customWorkout(item.workout)
                try await scheduler.schedule(WorkoutPlan(.custom(custom), id: uuid), at: when)
                owned[item.id] = item.rev
                free -= 1
                scheduledCount += 1
            } catch {
                errors.append(.init(id: item.id, message: error.localizedDescription))
            }
        }
        MCPSyncSettings.owned = owned

        // 3. Report back.
        let onWatch = upcoming.filter { owned[$0.id] == $0.rev }
            .map { MCPSyncStatus.Entry(id: $0.id, rev: $0.rev, date: $0.date) }
        let status = MCPSyncStatus(app: "HoodaRun",
                                   synced_at: ISO8601DateFormatter().string(from: Date()),
                                   max_scheduled: cap, scheduled: onWatch,
                                   pending: pending, errors: errors)
        let enc = JSONEncoder()
        enc.outputFormatting = [.prettyPrinted, .sortedKeys]
        if let source {
            try? await source.writeStatus(try enc.encode(status))   // reporting is best-effort
        }
        await CloudKitPlanStore.markScheduled(cloudPairs, owned: owned)

        return Outcome(scheduled: scheduledCount, removed: removed, pending: pending.count,
                      errors: errors.map(\.message))
    }

    private static func isoDay(_ d: Date) -> String {
        let f = DateFormatter()
        f.calendar = Calendar(identifier: .gregorian)
        f.locale = Locale(identifier: "en_US_POSIX")
        f.timeZone = .current
        f.dateFormat = "yyyy-MM-dd"
        return f.string(from: d)
    }
}
