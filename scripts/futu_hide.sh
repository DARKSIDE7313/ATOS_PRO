#!/bin/bash
while true; do
    osascript -e '
        tell application "System Events"
            if exists process "Futu_OpenD" then
                set visible of process "Futu_OpenD" to false
            end if
        end tell
    ' 2>/dev/null
    sleep 5
done
