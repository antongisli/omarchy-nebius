-- Optional user bindings. Check conflicts with `omarchy menu keybindings --print`.
-- Add these to ~/.config/hypr/bindings.lua, never to packaged Omarchy defaults.
local nebius_ui = os.getenv("HOME") .. "/.config/omarchy/plugins/nebius/bin/nebius-ui"
o.bind("SUPER + CTRL + G", "Nebius: get a GPU VM", "omarchy-launch-tui --app-id=org.nebius.manager " .. nebius_ui .. " get")
o.bind("SUPER + CTRL + J", "Nebius: jump into a VM", "omarchy-launch-tui --app-id=org.nebius.manager " .. nebius_ui .. " jump")
o.bind("SUPER + CTRL + M", "Nebius: VM manager", "omarchy-launch-tui --app-id=org.nebius.manager " .. nebius_ui .. " home")
