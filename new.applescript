on run argv
	set theHandle to item 1 of argv
	set theText to item 2 of argv
	tell application "Messages"
		try
			send theText to participant theHandle of (1st account whose service type = iMessage)
			return "iMessage"
		end try
		-- Non-Apple number: fall back to SMS relay via the paired iPhone.
		send theText to participant theHandle of (1st account whose service type = SMS)
		return "SMS"
	end tell
end run
