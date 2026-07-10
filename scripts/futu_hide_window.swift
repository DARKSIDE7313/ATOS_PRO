#!/usr/bin/env swift
// FutuOpenD 窗口隐藏守护者
// 每5秒检查并隐藏FutuOpenD窗口
// 编译: swiftc -o futu_hide futu_hide_window.swift

import Cocoa

let hideInterval: TimeInterval = 5.0

func hideFutuWindows() {
    let apps = NSWorkspace.shared.runningApplications
    for app in apps {
        if app.localizedName == "Futu_OpenD" ||
           (app.bundleIdentifier ?? "").contains("futu") ||
           (app.bundleIdentifier ?? "").contains("Futu") {
            // 隐藏这个应用
            app.hide()
        }
    }
}

print("[futu-hide] 启动 — 每\(hideInterval)秒隐藏FutuOpenD窗口")

while true {
    hideFutuWindows()
    Thread.sleep(forTimeInterval: hideInterval)
}
