# Streaming LAN Cast: Privacy Policy

_Last updated: 2026-06-26_

**Short version:** Streaming LAN Cast does not collect, store, sell, or transmit any of your
personal data to the developer or to any third party. Everything it does stays on
your own computer and your own local network.

## What the extension accesses

When (and only when) you click **Cast this tab** (or change the quality while
casting), Streaming LAN Cast reads the current tab's URL and title using the `activeTab`
permission. It uses these solely to tell the local helper which stream to cast and
what to display.

Without source detection enabled (see below), Streaming LAN Cast does not read other
tabs, your browsing history, your bookmarks, your form data, your cookies, or the
contents of web pages.

### Source detection (optional, off by default)

Some sites stream from a media URL that cannot be derived from the page address. To cast
those, you can turn on source detection from the popup, which grants the optional
`webRequest` + `<all_urls>` permission. While it is enabled, the extension passively
watches the media-manifest network requests (HLS/DASH/direct-video) on the sites you
visit and records, in a small in-memory list per tab (bounded, and cleared on
navigation, tab close, or when you revoke the permission):

- the detected media (manifest) URLs, and
- the request headers the browser already sent for those requests (Referer, Origin,
  User-Agent, and Cookie), which the local helper needs to replay to fetch a
  protected stream.

This capture happens only while source detection is enabled. The collected URLs and
headers (including any Cookie) are sent only to your local helper, are used solely to
fetch the stream you chose to cast, and never leave your computer. You can turn source
detection off at any time by removing the permission.

## Where that data goes

Everything the extension sends (the tab URL and title, and, only if you enabled source
detection, the detected media URL and its replay headers) goes only to a local
helper program running on your own machine at `http://127.0.0.1:9988` (the loopback
address, so it never leaves your computer). This is why the only required host
permission is `http://127.0.0.1:9988/*`; the optional `<all_urls>` host permission is
requested only if you turn on source detection.

Nothing is ever sent to the extension's developer, to Mozilla or Google, to any analytics
service, or to any remote server. There is no telemetry, no tracking, no
advertising, and no fingerprinting.

## Data stored on your device

The extension stores the following in your browser's local extension storage
(`storage.local`), on your device only. It is never transmitted anywhere:

- the helper token you paste in the options (a per-install secret used to
  authenticate the extension to your local helper),
- your theme preference (auto / light / dark),
- the last device you selected, so it can be pre-selected next time.

You can clear all of it at any time by removing the extension or clearing its
storage.

## The local helper

A separate, source-available helper program that you run on your own computer handles
the casting. The helper:

- uses [streamlink](https://streamlink.github.io/) to fetch the live stream from the
  site you chose to cast, and
- sends that stream to the DLNA or Google Cast device you select on your local
  network (e.g. your TV), using standard UPnP/DLNA or the Google Cast protocol.

These connections are made by your own machine, to destinations you choose, and are
under your control. The helper does not report anything back to the developer. Its
authentication token is generated locally and stored only on your machine
(`~/.streaming-lan-cast/token`).

## Permissions, and why each is needed

- `activeTab`: to read the URL/title of the tab you explicitly choose to cast.
- `storage`: to remember the helper token, theme, and last device locally.
- `clipboardRead`: only for the "Paste" button on the options page, which reads the
  clipboard once so you can drop in the helper token without typing it. The clipboard
  contents go nowhere except that local token field.
- `host_permissions: http://127.0.0.1:9988/*`: to talk to the local helper on
  the loopback address.
- `webRequest` + `<all_urls>` (optional, off by default): requested only if you
  enable source detection from the popup. Used to detect the media-manifest URL and
  the replay headers (Referer / Origin / User-Agent / Cookie) of the stream on the tab
  you cast, so the local helper can fetch it. This data is sent only to the local helper
  and the permission can be revoked at any time.

## Children's privacy

Streaming LAN Cast collects no data from anyone, including children.

## Changes to this policy

If this policy ever changes, the updated version will be published alongside the
extension with a new "Last updated" date.

## Contact

Questions about this policy can be sent to: fabriziosantidev@gmail.com
