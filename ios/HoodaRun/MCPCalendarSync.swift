//  MCPCalendarSync.swift — HoodaRun
//
//  Mirrors the workouts HoodaRun syncs to the Watch into a "HoodaRun" calendar
//  in Apple Calendar (iCloud when available, so it shows on every device).
//  Called at the end of MCPPlanSync.sync() with the same upcoming list, so
//  calendar and Watch never disagree about what's planned.
//
//  * New or changed workouts (by feed `rev`) create/update their event.
//  * Upcoming workouts that left the feed have their event removed.
//  * Past events are left alone — the calendar keeps your training history.
//  * Only events this file created are touched; anything you add to the
//    HoodaRun calendar yourself is ignored.
//
//  Info.plist: add NSCalendarsFullAccessUsageDescription, e.g.
//    "HoodaRun adds your planned workouts to Apple Calendar."
//  Requires iOS 17+ for requestFullAccessToEvents().

import CoreGraphics
import EventKit
import Foundation

actor MCPCalendarSync {
    static let shared = MCPCalendarSync()

    private let store = EKEventStore()
    private static let enabledKey = "mcp.calendar.enabled"
    private static let calendarKey = "mcp.calendar.identifier"
    private static let ownedKey = "mcp.calendar.owned"

    /// One stored entry per workout id.
    private struct Owned: Codable { let eventID: String; let rev: String; let date: String }

    static var isEnabled: Bool {
        get { UserDefaults.standard.bool(forKey: enabledKey) }
        set { UserDefaults.standard.set(newValue, forKey: enabledKey) }
    }

    struct Outcome { let added: Int; let updated: Int; let removed: Int }

    /// Ask once, when the user flips the toggle on.
    func requestAccess() async throws -> Bool {
        try await store.requestFullAccessToEvents()
    }

    func sync(_ upcoming: [MCPFeedWorkout], today: String) async throws -> Outcome {
        guard Self.isEnabled else { return Outcome(added: 0, updated: 0, removed: 0) }
        guard EKEventStore.authorizationStatus(for: .event) == .fullAccess else {
            throw MCPSyncError.unsupported("Allow HoodaRun full Calendar access in Settings.")
        }
        let calendar = try hoodaRunCalendar()
        var owned = loadOwned()
        let wanted = Dictionary(upcoming.map { ($0.id, $0) }, uniquingKeysWith: { a, _ in a })
        var added = 0, updated = 0, removed = 0

        // 1. Upcoming events whose workout left the feed.
        for (id, entry) in owned where entry.date >= today && wanted[id] == nil {
            if let event = store.event(withIdentifier: entry.eventID) {
                try store.remove(event, span: .thisEvent, commit: false)
                removed += 1
            }
            owned[id] = nil
        }

        // 2. New or changed workouts.
        for item in upcoming {
            let existing = owned[item.id].flatMap { store.event(withIdentifier: $0.eventID) }
            if let existing, owned[item.id]?.rev == item.rev, existing.calendar?.calendarIdentifier == calendar.calendarIdentifier {
                continue
            }
            let event = existing ?? EKEvent(eventStore: store)
            guard fill(event, from: item, in: calendar) else { continue }
            try store.save(event, span: .thisEvent, commit: false)
            owned[item.id] = Owned(eventID: event.eventIdentifier, rev: item.rev, date: item.date)
            if existing == nil { added += 1 } else { updated += 1 }
        }

        try store.commit()
        // Forget past entries after a while so the map stays small.
        owned = owned.filter { $0.value.date >= Self.day(offset: -90) }
        saveOwned(owned)
        return Outcome(added: added, updated: updated, removed: removed)
    }

    // MARK: - Event content

    private func fill(_ event: EKEvent, from item: MCPFeedWorkout, in calendar: EKCalendar) -> Bool {
        guard let comps = MCPWorkoutBuilder.dateComponents(item),
              let start = Calendar.current.date(from: comps) else { return false }
        event.calendar = calendar
        event.title = (item.sport == "strength" ? "🏋️ " : "🏃 ") + item.name
        event.notes = Self.describe(item)
        if item.time == nil {
            event.isAllDay = true
            event.startDate = Calendar.current.startOfDay(for: start)
            event.endDate = event.startDate
            event.availability = .free
        } else {
            event.isAllDay = false
            event.startDate = start
            let secs = item.workout.estimated_seconds ?? 3600
            event.endDate = start.addingTimeInterval(TimeInterval(secs))
            event.availability = .busy
        }
        event.url = URL(string: "hoodarun://workout/\(item.id)")
        return true
    }

    static func describe(_ item: MCPFeedWorkout) -> String {
        var lines: [String] = []
        if let plan = item.plan { lines.append("Plan: \(plan)") }
        if let d = item.description { lines.append(d) }
        let w = item.workout
        if let s = w.warmup { lines.append(step("Warm-up", s)) }
        for block in w.blocks {
            if block.iterations > 1 { lines.append("\(block.iterations)×") }
            for i in block.steps {
                let label = i.purpose == "work" ? "Work" : "Recover"
                lines.append((block.iterations > 1 ? "  " : "") + step(label, i.step))
            }
        }
        if let s = w.cooldown { lines.append(step("Cool-down", s)) }
        lines.append("Scheduled on Apple Watch by HoodaRun.")
        return lines.joined(separator: "\n")
    }

    private static func step(_ label: String, _ s: MCPStep) -> String {
        if let name = s.display_name { return "\(label): \(name)" }
        return "\(label): \(goal(s.goal))\(alert(s.alert))"
    }

    private static func goal(_ g: MCPGoal) -> String {
        switch g.type {
        case "time":
            let s = Int((g.seconds ?? 0).rounded())
            return s % 60 == 0 ? "\(s / 60) min" : String(format: "%d:%02d", s / 60, s % 60)
        case "distance":
            let m = g.meters ?? 0
            let mi = m / 1609.344
            return mi >= 0.25 && abs(mi - (mi * 100).rounded() / 100) < 0.005
                ? String(format: "%g mi", (mi * 100).rounded() / 100)
                : "\(Int(m.rounded())) m"
        default:
            return "open"
        }
    }

    private static func alert(_ a: MCPAlert?) -> String {
        guard let a else { return "" }
        if a.type == "hr_zone", let z = a.zone { return " @ HR zone \(z)" }
        if a.type == "speed", let lo = a.min_mps, let hi = a.max_mps, lo > 0, hi > 0 {
            return " @ \(pace(hi))–\(pace(lo))/mi"   // faster speed = quicker pace
        }
        return ""
    }

    private static func pace(_ mps: Double) -> String {
        let s = Int((1609.344 / mps).rounded())
        return String(format: "%d:%02d", s / 60, s % 60)
    }

    // MARK: - Calendar + storage

    private func hoodaRunCalendar() throws -> EKCalendar {
        if let id = UserDefaults.standard.string(forKey: Self.calendarKey),
           let cal = store.calendar(withIdentifier: id) {
            return cal
        }
        let cal = EKCalendar(for: .event, eventStore: store)
        cal.title = "HoodaRun"
        cal.cgColor = CGColor(red: 1.0, green: 0.42, blue: 0.17, alpha: 1)
        let sources = store.sources
        cal.source = sources.first { $0.sourceType == .calDAV && $0.title == "iCloud" }
            ?? store.defaultCalendarForNewEvents?.source
            ?? sources.first { $0.sourceType == .local }
        guard cal.source != nil else {
            throw MCPSyncError.unsupported("No calendar account available on this iPhone.")
        }
        try store.saveCalendar(cal, commit: true)
        UserDefaults.standard.set(cal.calendarIdentifier, forKey: Self.calendarKey)
        return cal
    }

    private func loadOwned() -> [String: Owned] {
        guard let data = UserDefaults.standard.data(forKey: Self.ownedKey) else { return [:] }
        return (try? JSONDecoder().decode([String: Owned].self, from: data)) ?? [:]
    }

    private func saveOwned(_ owned: [String: Owned]) {
        UserDefaults.standard.set(try? JSONEncoder().encode(owned), forKey: Self.ownedKey)
    }

    private static func day(offset: Int) -> String {
        let f = DateFormatter()
        f.calendar = Calendar(identifier: .gregorian)
        f.locale = Locale(identifier: "en_US_POSIX")
        f.dateFormat = "yyyy-MM-dd"
        return f.string(from: Calendar.current.date(byAdding: .day, value: offset, to: Date())!)
    }
}
