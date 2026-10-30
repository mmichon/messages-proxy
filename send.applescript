on run argv
	set theGuid to item 1 of argv
	set theText to item 2 of argv
	tell application "Messages"
		send theText to chat id theGuid
	end tell
end run
