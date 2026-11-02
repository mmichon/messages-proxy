on run argv
	set theGuid to item 1 of argv
	-- Resolve the path outside the Messages tell block; inside it, `POSIX file`
	-- is looked up in Messages' dictionary and fails.
	set theFile to POSIX file (item 2 of argv)
	tell application "Messages"
		send theFile to chat id theGuid
	end tell
end run
