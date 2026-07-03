# Custom Google Cast receiver

A branded Google Cast receiver for Streaming LAN Cast. When a cast device runs this receiver, the
stream plays with the Streaming LAN Cast logo and player instead of the plain Default Media
Receiver. It only affects Google Cast devices (Chromecast built-in, including some smart TVs such as
this Samsung); DLNA renderers are unaffected.

Files: `index.html` (the receiver) and `icon-256.png` (splash/logo).

## What it needs (one-time)

1. HTTPS hosting for this `receiver/` folder (Google requires HTTPS). Some options:
   - a separate dev repo's GitHub Pages,
   - Netlify or Cloudflare Pages (free static hosting),
   - a quick tunnel for testing: `cloudflared tunnel --url http://localhost:8000` while serving this
     folder with `python -m http.server 8000`.
2. A Google Cast developer account at https://cast.google.com/publish (one-time USD 5 fee):
   - Add a new Custom Receiver pointing at your HTTPS receiver URL. It gives you an App ID.
   - Register your test device (the Samsung / Chromecast) under "Cast Receiver Devices" so it can
     load the unpublished receiver during development, then reboot the device.
   - Publish the receiver once you are happy (after that, any device can use it).

## Run the helper with it

Two environment variables switch the helper to the branded receiver:

    SLC_CAST_APP_ID=<your App ID>   # use this receiver instead of the Default Media Receiver
    SLC_PREFER_CAST=1               # cast via Google Cast to devices that also expose DLNA (the Samsung)

Quick test, running the helper directly:

    SLC_CAST_APP_ID=XXXXXXXX SLC_PREFER_CAST=1 python helper/streaming-lan-cast-helper.py --serve

For the installed systemd user service, add them as a drop-in:

    systemctl --user edit streaming-lan-cast.service
    #   [Service]
    #   Environment=SLC_CAST_APP_ID=XXXXXXXX
    #   Environment=SLC_PREFER_CAST=1
    systemctl --user restart streaming-lan-cast.service

Then cast a tab as usual. On a Cast device the stream now loads inside the branded receiver.

## Notes

- `SLC_PREFER_CAST=1` is only needed for devices that expose both DLNA and Cast (the helper prefers
  DLNA by default). Cast-only devices use Cast regardless.
- No effect on DLNA-only renderers.
- Unregistered receivers only load on devices you registered as test devices; publish the receiver
  in the console to reach everyone.
