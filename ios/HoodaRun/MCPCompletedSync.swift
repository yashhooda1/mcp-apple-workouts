//  MCPCompletedSync.swift — HoodaRun
//
//  The other half of the loop. MCPPlanSync sends plans to the Watch; this
//  reads what the runner actually did from HealthKit and reports it to the
//  apple-workouts-mcp server, so the assistant can see how the week went and
//  build the next plan from real training instead of a guess.
//
//  Opt-in (Claude plans › Share completed workouts). Health data leaves the
//  phone only when that switch is on, and goes only to the feed source the
//  runner configured: POST /completed on their server, or completed.json in
//  their iCloud Drive folder.
//
//  What is sent, per workout from the last 90 days: HealthKit's UUID, type,
//  local start/end time, duration, distance, average and max heart rate,
//  elevation gain, and the recording source's name. No routes, no samples.
//  The server merges by UUID, so sending the same window every sync is safe.
//
//  Requirements: iOS 16+ (HKWorkout.statistics(for:)), the HealthKit
//  capability, and NSHealthShareUsageDescription in Info.plist.

import Foundation
import HealthKit

struct MCPCompletedWorkout: Codable {
    let id: String
    let type: String
    let start: String          // local wall-clock time, no zone: "2026-11-16T06:02:11"
    let end: String
    let duration_s: Double
    let distance_m: Double?
    let avg_hr: Double?
    let max_hr: Double?
    let elevation_gain_m: Double?
    let source: String
}

struct MCPCompletedPayload: Codable {
    let schema: String
    let app: String
    let reported_at: String
    let workouts: [MCPCompletedWorkout]
}

enum MCPCompletedSync {
    private static let enabledKey = "mcp.completed.enabled"
    static let windowDays = 90

    static var isEnabled: Bool {
        get { UserDefaults.standard.bool(forKey: enabledKey) }
        set { UserDefaults.standard.set(newValue, forKey: enabledKey) }
    }

    private static let store = HKHealthStore()

    private static var readTypes: Set<HKObjectType> {
        [HKObjectType.workoutType(),
         HKQuantityType(.heartRate),
         HKQuantityType(.distanceWalkingRunning),
         HKQuantityType(.distanceCycling)]
    }

    /// Ask once, when the runner flips the switch on. HealthKit never says
    /// whether read access was granted, so this only reports that the sheet
    /// could be shown; a denial simply yields no workouts.
    static func requestAccess() async -> Bool {
        guard HKHealthStore.isHealthDataAvailable() else { return false }
        do {
            try await store.requestAuthorization(toShare: [], read: readTypes)
            return true
        } catch {
            return false
        }
    }

    /// The JSON body to report, or nil when sharing is off or Health is unavailable.
    static func payload() async -> Data? {
        guard isEnabled, HKHealthStore.isHealthDataAvailable() else { return nil }
        guard let workouts = try? await collect() else { return nil }
        let body = MCPCompletedPayload(schema: "hooda.workouts.completed/v1", app: "HoodaRun",
                                       reported_at: ISO8601DateFormatter().string(from: Date()),
                                       workouts: workouts)
        return try? JSONEncoder().encode(body)
    }

    static func collect() async throws -> [MCPCompletedWorkout] {
        let since = Calendar.current.date(byAdding: .day, value: -windowDays, to: Date()) ?? Date()
        let predicate = HKQuery.predicateForSamples(withStart: since, end: nil,
                                                    options: .strictStartDate)
        let descriptor = HKSampleQueryDescriptor(
            predicates: [.workout(predicate)],
            sortDescriptors: [SortDescriptor(\.startDate, order: .reverse)],
            limit: 400)
        let found = try await descriptor.result(for: store)
        return found.map(row)
    }

    private static func row(_ w: HKWorkout) -> MCPCompletedWorkout {
        let cycling = w.workoutActivityType == .cycling
        let distanceType = HKQuantityType(cycling ? .distanceCycling : .distanceWalkingRunning)
        let bpm = HKUnit.count().unitDivided(by: .minute())
        let heart = w.statistics(for: HKQuantityType(.heartRate))
        let climb = w.metadata?[HKMetadataKeyElevationAscended] as? HKQuantity
        return MCPCompletedWorkout(
            id: w.uuid.uuidString,
            type: typeName(w.workoutActivityType),
            start: local(w.startDate),
            end: local(w.endDate),
            duration_s: w.duration,                       // excludes pauses
            distance_m: w.statistics(for: distanceType)?.sumQuantity()?.doubleValue(for: .meter()),
            avg_hr: heart?.averageQuantity()?.doubleValue(for: bpm),
            max_hr: heart?.maximumQuantity()?.doubleValue(for: bpm),
            elevation_gain_m: climb?.doubleValue(for: .meter()),
            source: w.sourceRevision.source.name)
    }

    /// Names the server's completed.py recognises.
    private static func typeName(_ t: HKWorkoutActivityType) -> String {
        switch t {
        case .running: return "running"
        case .walking: return "walking"
        case .hiking: return "hiking"
        case .cycling: return "cycling"
        case .traditionalStrengthTraining, .functionalStrengthTraining, .coreTraining:
            return "strength"
        default: return "other"
        }
    }

    private static func local(_ d: Date) -> String {
        let f = DateFormatter()
        f.calendar = Calendar(identifier: .gregorian)
        f.locale = Locale(identifier: "en_US_POSIX")
        f.timeZone = .current
        f.dateFormat = "yyyy-MM-dd'T'HH:mm:ss"
        return f.string(from: d)
    }
}
