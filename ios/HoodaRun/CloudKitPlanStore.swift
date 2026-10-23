//  CloudKitPlanStore.swift — HoodaRun
//
//  Reads workouts saved from the yashhooda.ai builder (CloudKit JS) out of the
//  signed-in user's PRIVATE CloudKit database, and marks each one with the
//  revision HoodaRun scheduled so the website can show "On Watch".
//
//  Record type `PlannedWorkout` (create it in CloudKit Console, or let the
//  first save in the Development environment create it, then deploy the
//  schema to Production):
//
//      date          String   YYYY-MM-DD   — Queryable + Sortable index
//      time          String   HH:MM or ""
//      name          String
//      sport         String   running | strength
//      workoutJSON   String   hooda.workouts.custom/v1 (same as the MCP feed)
//      rev           String   content hash; changes when the workout changes
//      source        String   web | mcp
//      scheduledRev  String   written here: the rev now on the Watch
//      syncedAt      Date/Time  written here
//
//  Also add a Queryable index on `recordName` (CloudKit requires it for
//  queries). Signing & Capabilities: iCloud › CloudKit, container below.

import CloudKit
import Foundation

enum CloudKitPlanStore {
    static let containerID = "iCloud.ai.yashhooda.HoodaRun"
    static let recordType = "PlannedWorkout"
    private static let enabledKey = "mcp.cloudkit.enabled"

    static var database: CKDatabase {
        CKContainer(identifier: containerID).privateCloudDatabase
    }

    /// On by default; a toggle in settings can turn it off.
    static var isEnabled: Bool {
        get { UserDefaults.standard.object(forKey: enabledKey) as? Bool ?? true }
        set { UserDefaults.standard.set(newValue, forKey: enabledKey) }
    }

    static func accountAvailable() async -> Bool {
        (try? await CKContainer(identifier: containerID).accountStatus()) == .available
    }

    /// Upcoming workouts (date >= `day`), with the CKRecord kept for write-back.
    static func upcoming(from day: String) async throws -> [(item: MCPFeedWorkout, record: CKRecord)] {
        let query = CKQuery(recordType: recordType,
                            predicate: NSPredicate(format: "date >= %@", day))
        query.sortDescriptors = [NSSortDescriptor(key: "date", ascending: true)]

        var records: [CKRecord] = []
        var (page, cursor) = try await database.records(matching: query, resultsLimit: 200)
        while true {
            for (_, result) in page {
                if case .success(let rec) = result { records.append(rec) }
            }
            guard let next = cursor else { break }
            (page, cursor) = try await database.records(continuingMatchFrom: next, resultsLimit: 200)
        }

        let decoder = JSONDecoder()
        return records.compactMap { rec in
            guard let date = rec["date"] as? String,
                  let rev = rec["rev"] as? String,
                  let name = rec["name"] as? String,
                  let json = (rec["workoutJSON"] as? String)?.data(using: .utf8),
                  let workout = try? decoder.decode(MCPCustomWorkout.self, from: json)
            else { return nil }   // malformed records are skipped, never scheduled
            let time = (rec["time"] as? String).flatMap { $0.isEmpty ? nil : $0 }
            let item = MCPFeedWorkout(id: rec.recordID.recordName.uppercased(), rev: rev,
                                      date: date, time: time, name: name,
                                      sport: rec["sport"] as? String ?? "running",
                                      plan: nil, description: nil, workout: workout)
            return (item, rec)
        }
    }

    /// Write `scheduledRev` back only where it changed. Best-effort.
    static func markScheduled(_ pairs: [(item: MCPFeedWorkout, record: CKRecord)],
                              owned: [String: String]) async {
        let now = Date()
        let changed: [CKRecord] = pairs.compactMap { pair in
            let onWatch = owned[pair.item.id] == pair.item.rev ? pair.item.rev : ""
            guard (pair.record["scheduledRev"] as? String ?? "") != onWatch else { return nil }
            pair.record["scheduledRev"] = onWatch
            pair.record["syncedAt"] = now
            return pair.record
        }
        guard !changed.isEmpty else { return }
        _ = try? await database.modifyRecords(saving: changed, deleting: [],
                                              savePolicy: .changedKeys)
    }
}
