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

The helper uses this receiver by default (App ID `C4B6F8FF`) and prefers Cast over DLNA when a device
exposes both, so casting to a Cast device just works with no configuration.

Overrides (environment variables), for development or a different receiver:

    SLC_CAST_APP_ID=XXXXXXXX   # use a different receiver App ID (e.g. CC1AD845 = Default Media Receiver)
    SLC_PREFER_DLNA=1          # force DLNA (MPEG-TS/SOAP) on devices that also expose Cast

Quick test with a different receiver, running the helper directly:

    SLC_CAST_APP_ID=XXXXXXXX python helper/streaming-lan-cast-helper.py --serve

For the installed systemd user service, add an override as a drop-in:

    systemctl --user edit streaming-lan-cast.service
    #   [Service]
    #   Environment=SLC_CAST_APP_ID=XXXXXXXX
    systemctl --user restart streaming-lan-cast.service

## Notes

- The helper prefers Cast when a device exposes both DLNA and Cast; `SLC_PREFER_DLNA=1` forces the
  plain DLNA player instead (e.g. when a source plays back better over DLNA).
- DLNA-only renderers are not affected; they always use DLNA.
- A custom receiver only loads on devices you registered as test devices until you publish it in the
  console; after that, any device can use it.
