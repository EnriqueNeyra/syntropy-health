"""
The Mac's sign-in sheet: another site (a health system's or a wearable's sign-in page) opens in a sheet attached to the
window, so the window never leaves Syntropy Health and its chrome never changes.

- The sheet has its own web view. It shares the window's website data, so the callback finds the same session, but none
  of its message handlers or pywebview's: a page in it has nothing to post to and can't reach the app (sites.py).
- The bar above the page always says where you are, in native views no page can draw over. The name changes when a page
  commits, not when a navigation starts: otherwise a page could send itself to another address and draw a fake of it
  while the bar names that address.
- The page stays covered until it has something to draw, so nothing flashes white. It's covered again when the address
  comes back to this server, while the callback does its work.
- When the address reaches one of the app's own pages the sheet closes and the window goes there without reloading, so
  the page's existing handling of connected or problem runs. Cancel closes it and stays where you were.

Everything here runs on the main thread.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

import AppKit
import objc
import sites
import WebKit
from Foundation import NSObject
from PyObjCTools import AppHelper

log = logging.getLogger("syntropy.desktop")

BIGGEST = (860, 780)                 # as big as the sheet gets: a sign-in is one narrow column, however large the window
WINDOW_SHOWS = (48, 88)              # how much of the window stays in sight around it (the height includes the title bar)
BAR_HEIGHT = 44
FADE = 0.18                          # seconds
DRAWN_CHECK = 0.06                   # how often to ask whether a page that has committed has anything to draw, in seconds
DRAWN_AT_THE_LATEST = 2.0            # a page may take as long as it likes to paint anything; show what it has by then
FILES_REFUSED = "Files can't be downloaded during sign-in."
# Navigations that were stopped rather than failed: a newer one replaced them, or this module cancelled them.
NOT_FAILURES = {-999, 102}


def url_text(url: Any) -> Optional[str]:
    return str(url.absoluteString()) if url is not None else None


class _SheetWindow(AppKit.NSWindow):
    """⌘W means Cancel: without a close button AppKit would only beep."""

    def performClose_(self, _sender):
        self.sign_in.cancel()


class _SheetEvents(NSObject):
    """What AppKit and key-value observing tell the sheet, and what its buttons do."""

    def initWithSheet_(self, sheet):
        self = objc.super(_SheetEvents, self).init()
        if self is not None:
            self.sheet = sheet
        return self

    def observeValueForKeyPath_ofObject_change_context_(self, _path, _obj, _change, _context):
        self.sheet.progress_changed()

    def windowResized_(self, _note):
        self.sheet.fit()

    def cancelPressed_(self, _sender):
        self.sheet.cancel()

    def backPressed_(self, _sender):
        self.sheet.webview.goBack_(None)

    def tryAgainPressed_(self, _sender):
        self.sheet.try_again()


class _SheetPage(NSObject):
    """The web view's delegate. A decision has to be given, whatever goes wrong on the way to it, or the page hangs."""

    def initWithSheet_(self, sheet):
        self = objc.super(_SheetPage, self).init()
        if self is not None:
            self.sheet = sheet
        return self

    def webView_decidePolicyForNavigationAction_decisionHandler_(self, _webview, action, handler):
        try:
            handler(self.sheet.decide(action))
        except Exception:  # noqa: BLE001
            log.exception("Couldn't decide on a navigation in the sign-in sheet")
            handler(WebKit.WKNavigationActionPolicyCancel)

    def webView_decidePolicyForNavigationResponse_decisionHandler_(self, _webview, response, handler):
        try:
            handler(self.sheet.decide_response(response))
        except Exception:  # noqa: BLE001
            log.exception("Couldn't decide on a response in the sign-in sheet")
            handler(WebKit.WKNavigationResponsePolicyCancel)

    def webView_didCommitNavigation_(self, _webview, _navigation):
        self.sheet.committed()

    def webView_didFinishNavigation_(self, _webview, _navigation):
        self.sheet.finished()

    def webView_didFailProvisionalNavigation_withError_(self, _webview, _navigation, error):
        self.sheet.failed(error)

    def webView_didFailNavigation_withError_(self, _webview, _navigation, error):
        self.sheet.failed(error)

    def webViewWebContentProcessDidTerminate_(self, webview):
        webview.reload()        # it leaves an empty page otherwise

    def webView_createWebViewWithConfiguration_forNavigationAction_windowFeatures_(self, _webview, _config, action, _features):
        self.sheet.new_window(action)
        return None

    def webView_runJavaScriptAlertPanelWithMessage_initiatedByFrame_completionHandler_(self, _webview, message, frame, handler):
        self.sheet.ask(frame, message, ["OK"], lambda _ok, _text: handler())

    def webView_runJavaScriptConfirmPanelWithMessage_initiatedByFrame_completionHandler_(self, _webview, message, frame, handler):
        self.sheet.ask(frame, message, ["OK", "Cancel"], lambda ok, _text: handler(ok))

    def webView_runJavaScriptTextInputPanelWithPrompt_defaultText_initiatedByFrame_completionHandler_(
            self, _webview, prompt, default, frame, handler):
        self.sheet.ask(frame, prompt, ["OK", "Cancel"], lambda ok, text: handler(text if ok else None), default or "")

    def webView_runOpenPanelWithParameters_initiatedByFrame_completionHandler_(self, _webview, parameters, _frame, handler):
        self.sheet.choose_files(parameters, handler)


class SignInSheet:
    def __init__(self, parent: Any, main_view: Any, home: Callable[[], set[sites.Origin]], person_acted: Callable[[], bool],
                 on_end: Callable[[Optional[str]], None]):
        self.parent = parent                     # the NSWindow
        self.home = home
        self.person_acted = person_acted
        self.leave = sites.only_when_clicked(person_acted).open
        self.on_end = on_end                     # given the hash to show ("" or None: stay where you were)
        self.closed = False
        self.covered = True
        self.ever_committed = False
        self.turn = 0                            # says which page a check that was waiting is about: a newer page, or the
                                                 # cover going back up for one on its way, ends the check
        self.last_request: Any = None
        self.dialogs: list[Any] = []             # windows open over the sheet, each holding up its page until it's answered
        self.events = _SheetEvents.alloc().initWithSheet_(self)
        self.page = _SheetPage.alloc().initWithSheet_(self)      # WebKit doesn't retain its delegates

        config = WebKit.WKWebViewConfiguration.alloc().init()    # its own content controller: no message handlers
        config.setWebsiteDataStore_(main_view.configuration().websiteDataStore())
        config.setProcessPool_(main_view.configuration().processPool())
        width, height = self.size()
        self.window = _SheetWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            ((0, 0), (width, height)), AppKit.NSWindowStyleMaskTitled, AppKit.NSBackingStoreBuffered, False)
        self.window.sign_in = self
        self.window.setTitle_("Sign in")         # not shown on a sheet, but VoiceOver and the like read it
        self.window.setReleasedWhenClosed_(False)
        self.match_appearance()
        self.webview = WebKit.WKWebView.alloc().initWithFrame_configuration_(((0, 0), (width, height - BAR_HEIGHT)), config)
        # What the site sees stays what it saw in the window: some refuse an embedded browser they don't recognise.
        self.webview.setCustomUserAgent_(main_view.customUserAgent())
        self.webview.setPageZoom_(main_view.pageZoom())
        self.webview.setAutoresizingMask_(AppKit.NSViewWidthSizable | AppKit.NSViewHeightSizable)
        self.webview.setNavigationDelegate_(self.page)
        self.webview.setUIDelegate_(self.page)
        self.window.contentView().addSubview_(self.webview)
        self._build_cover(width, height - BAR_HEIGHT)
        self._build_bar(width, height)
        for path in ("estimatedProgress", "loading", "canGoBack"):
            self.webview.addObserver_forKeyPath_options_context_(self.events, path, 0, None)
        AppKit.NSNotificationCenter.defaultCenter().addObserver_selector_name_object_(
            self.events, "windowResized:", AppKit.NSWindowDidResizeNotification, parent)

    # ----------------------------------------------------------------- what it looks like
    def size(self) -> tuple[float, float]:
        frame = self.parent.frame()
        return (min(BIGGEST[0], frame.size.width - WINDOW_SHOWS[0]), min(BIGGEST[1], frame.size.height - WINDOW_SHOWS[1]))

    def fit(self) -> None:
        self.window.setContentSize_(self.size())

    def match_appearance(self) -> None:
        self.window.setAppearance_(self.parent.appearance())

    def _build_bar(self, width: float, height: float) -> None:
        bar = AppKit.NSView.alloc().initWithFrame_(((0, height - BAR_HEIGHT), (width, BAR_HEIGHT)))
        bar.setAutoresizingMask_(AppKit.NSViewWidthSizable | AppKit.NSViewMinYMargin)
        self.window.contentView().addSubview_(bar)

        self.cancel_button = AppKit.NSButton.buttonWithTitle_target_action_("Cancel", self.events, "cancelPressed:")
        self.cancel_button.setKeyEquivalent_("\x1b")
        self.cancel_button.sizeToFit()
        cancel = self.cancel_button.frame().size
        self.cancel_button.setFrameOrigin_((width - cancel.width - 12, (BAR_HEIGHT - cancel.height) / 2))
        self.cancel_button.setAutoresizingMask_(AppKit.NSViewMinXMargin)
        bar.addSubview_(self.cancel_button)

        chevron = AppKit.NSImage.imageWithSystemSymbolName_accessibilityDescription_("chevron.backward", "Back")
        self.back_button = AppKit.NSButton.buttonWithImage_target_action_(chevron, self.events, "backPressed:")
        self.back_button.setBordered_(False)
        self.back_button.setFrame_(((10, (BAR_HEIGHT - 24) / 2), (24, 24)))
        self.back_button.setHidden_(True)
        bar.addSubview_(self.back_button)

        self.lock = AppKit.NSImageView.alloc().initWithFrame_(((14, (BAR_HEIGHT - 16) / 2), (16, 16)))
        self.lock.setContentTintColor_(AppKit.NSColor.secondaryLabelColor())
        bar.addSubview_(self.lock)

        self.name = AppKit.NSTextField.labelWithString_("")
        self.name.setFont_(AppKit.NSFont.systemFontOfSize_weight_(13, AppKit.NSFontWeightMedium))
        self.name.setLineBreakMode_(AppKit.NSLineBreakByTruncatingMiddle)
        self.name.setFrame_(((36, (BAR_HEIGHT - 18) / 2), (width - 36 - cancel.width - 28, 18)))
        self.name.setAutoresizingMask_(AppKit.NSViewWidthSizable)
        bar.addSubview_(self.name)

        line = AppKit.NSBox.alloc().initWithFrame_(((0, 0), (width, 1)))
        line.setBoxType_(AppKit.NSBoxSeparator)
        line.setAutoresizingMask_(AppKit.NSViewWidthSizable)
        bar.addSubview_(line)

        self.progress = AppKit.NSView.alloc().initWithFrame_(((0, 0), (0, 2)))
        self.progress.setWantsLayer_(True)
        self.progress.layer().setBackgroundColor_(AppKit.NSColor.controlAccentColor().CGColor())
        bar.addSubview_(self.progress)
        self.bar_view = bar

    def _build_cover(self, width: float, height: float) -> None:
        # A box rather than a coloured layer: it follows the sheet's appearance by itself.
        self.cover = AppKit.NSBox.alloc().initWithFrame_(((0, 0), (width, height)))
        self.cover.setBoxType_(AppKit.NSBoxCustom)
        self.cover.setBorderWidth_(0)
        self.cover.setFillColor_(AppKit.NSColor.windowBackgroundColor())
        self.cover.setContentViewMargins_((0, 0))
        self.cover.setAutoresizingMask_(AppKit.NSViewWidthSizable | AppKit.NSViewHeightSizable)
        self.window.contentView().addSubview_(self.cover)

        middle = height / 2
        keep_centred = AppKit.NSViewMinXMargin | AppKit.NSViewMaxXMargin | AppKit.NSViewMinYMargin | AppKit.NSViewMaxYMargin
        self.spinner = AppKit.NSProgressIndicator.alloc().initWithFrame_(((width / 2 - 10, middle + 12), (20, 20)))
        self.spinner.setStyle_(AppKit.NSProgressIndicatorStyleSpinning)
        self.spinner.setDisplayedWhenStopped_(False)
        self.spinner.setAutoresizingMask_(keep_centred)
        self.spinner.startAnimation_(None)

        self.words = AppKit.NSTextField.wrappingLabelWithString_("Opening sign-in…")
        self.words.setAlignment_(AppKit.NSTextAlignmentCenter)
        self.words.setTextColor_(AppKit.NSColor.secondaryLabelColor())
        self.words.setFrame_(((width / 2 - 200, middle - 44), (400, 48)))
        self.words.setAutoresizingMask_(keep_centred)

        self.try_again_button = AppKit.NSButton.buttonWithTitle_target_action_("Try Again", self.events, "tryAgainPressed:")
        self.try_again_button.sizeToFit()
        size = self.try_again_button.frame().size
        self.try_again_button.setFrameOrigin_((width / 2 - size.width / 2, middle - 44 - size.height))
        self.try_again_button.setAutoresizingMask_(keep_centred)
        self.try_again_button.setHidden_(True)
        for view in (self.spinner, self.words, self.try_again_button):
            self.cover.contentView().addSubview_(view)

    def _say_where(self, url: Optional[str]) -> None:
        name, lock = sites.sheet_bar(url, self.home())
        self.name.setStringValue_(name)
        self.name.setToolTip_(name)
        symbol = {"lock": "lock.fill", "open": "lock.open.fill"}.get(lock)
        words = {"lock": "Secure connection", "open": "Connection isn't encrypted"}.get(lock)
        self.lock.setImage_(AppKit.NSImage.imageWithSystemSymbolName_accessibilityDescription_(symbol, words) if symbol else None)
        self._lay_out_bar()

    def _lay_out_bar(self) -> None:
        x = 14
        can_go_back = bool(self.webview.canGoBack())
        self.back_button.setHidden_(not can_go_back)
        if can_go_back:
            x = 38
        self.lock.setFrameOrigin_((x, self.lock.frame().origin.y))
        self.lock.setHidden_(self.lock.image() is None)
        if self.lock.image() is not None:
            x += 22
        frame = self.name.frame()
        right = frame.origin.x + frame.size.width
        self.name.setFrame_(((x, frame.origin.y), (right - x, frame.size.height)))

    def progress_changed(self) -> None:
        if self.closed:
            return
        loading = bool(self.webview.isLoading())
        self.progress.setHidden_(not loading)
        if loading:
            self.progress.setFrameSize_((self.bar_view.frame().size.width * max(0.08, self.webview.estimatedProgress()), 2))
        self._lay_out_bar()

    def _reduce_motion(self) -> bool:
        return bool(AppKit.NSWorkspace.sharedWorkspace().accessibilityDisplayShouldReduceMotion())

    def _fade_cover(self, to: float, then: Optional[Callable[[], None]] = None) -> None:
        def changes(context: Any) -> None:
            context.setDuration_(0 if self._reduce_motion() else FADE)
            self.cover.animator().setAlphaValue_(to)

        AppKit.NSAnimationContext.runAnimationGroup_completionHandler_(changes, then)

    def _cover_with(self, words: str, trouble: bool = False) -> None:
        self.words.setStringValue_(words)
        self.try_again_button.setHidden_(not trouble)
        if trouble:
            self.spinner.stopAnimation_(None)
        else:
            self.spinner.startAnimation_(None)
        self.covered = True
        self.turn += 1
        self.cover.setHidden_(False)
        self._fade_cover(1.0)

    def _uncover(self) -> None:
        if self.closed or not self.covered:
            return
        self.covered = False

        def gone() -> None:
            if not self.covered:        # unless it was needed again in the meantime
                self.cover.setHidden_(True)
                self.spinner.stopAnimation_(None)

        self._fade_cover(0.0, gone)
        self.window.makeFirstResponder_(self.webview)

    # ----------------------------------------------------------------- opening and closing
    def open(self, request: Any) -> None:
        self._say_where(url_text(request.URL()))       # first: it's drawn as it slides in
        self.parent.beginSheet_completionHandler_(self.window, self._gone)
        self.load(request)

    def _gone(self, _response: int) -> None:
        # These hold each other through AppKit and WebKit, where Python can't see it, and a web view left over would keep
        # that page's process running. By now it has slid away and nothing can be pressed.
        self.webview.removeFromSuperview()
        self.window.sign_in = self.events.sheet = self.page.sheet = None

    def load(self, request: Any) -> None:
        if not self.ever_committed:
            self._say_where(url_text(request.URL()))
        self.last_request = request
        self.webview.loadRequest_(request)

    def try_again(self) -> None:
        self._cover_with("Opening sign-in…")
        self.webview.loadRequest_(self.last_request)

    def cancel(self) -> None:
        self._close(None)

    def _close(self, route: Optional[str]) -> None:
        if self.closed:
            return
        self.closed = True
        for dialog in list(self.dialogs):       # WebKit needs its answer before the page goes
            self.window.endSheet_(dialog)
        self.webview.stopLoading()
        for path in ("estimatedProgress", "loading", "canGoBack"):
            self.webview.removeObserver_forKeyPath_(self.events, path)
        AppKit.NSNotificationCenter.defaultCenter().removeObserver_(self.events)
        self.webview.setNavigationDelegate_(None)
        self.webview.setUIDelegate_(None)
        # The window's page first: what it draws is there as the sheet slides away.
        self.on_end(route)
        self.parent.endSheet_(self.window)

    # ----------------------------------------------------------------- navigation
    def decide(self, action: Any) -> int:
        url = url_text(action.request().URL())
        frame = action.targetFrame()
        main = frame is None or bool(frame.isMainFrame())
        may = sites.sheet_may(url)
        if action.shouldPerformDownload():
            self._refused(FILES_REFUSED, main, self.person_acted())
        elif may == "leave":
            self.leave(url)
        elif may == "refuse":
            log.warning("Refused a page's attempt to open another program from the sign-in sheet")
            self._refused("This page tried to open another program, which isn't allowed here.", main)
        elif not main:
            return WebKit.WKNavigationActionPolicyAllow
        else:
            home = self.home()
            ended = sites.sign_in_ended(url, home)
            if ended is not None:
                if not self.covered:
                    self._cover_with("Finishing…")
                # Not during WebKit's question: closing takes the web view apart.
                AppHelper.callAfter(self._close, ended)
                return WebKit.WKNavigationActionPolicyCancel
            self.last_request = action.request()
            if sites.origin(url) in home:
                self._cover_with("Finishing…")
            if not self.ever_committed:          # nothing of anyone's is showing, so the bar can follow the redirects
                self._say_where(url)
            return WebKit.WKNavigationActionPolicyAllow
        return WebKit.WKNavigationActionPolicyCancel

    def decide_response(self, response: Any) -> int:
        if response.canShowMIMEType():
            return WebKit.WKNavigationResponsePolicyAllow
        self._refused(FILES_REFUSED, bool(response.isForMainFrame()), self.person_acted())
        return WebKit.WKNavigationResponsePolicyCancel

    def new_window(self, action: Any) -> None:
        # A link meant for a new window opens here. A blank window that a script means to write into can't.
        url = url_text(action.request().URL())
        may = sites.sheet_may(url)
        if may == "leave":
            self.leave(url)
        elif sites.origin(url) is not None:
            self.load(action.request())

    def _refused(self, words: str, main: bool, a_person_asked: bool = False) -> None:
        # With nothing showing, the cover would otherwise say "Opening" for ever. With a page showing it carries on, and
        # only hears about it if a person chose the thing: a page shouldn't be able to put up box after box by script.
        if main and not self.ever_committed:
            self._cover_with(words, trouble=True)
        elif main and a_person_asked and not self.dialogs:
            self.ask(None, words, ["OK"], lambda _ok, _text: None)

    def committed(self) -> None:
        url = url_text(self.webview.URL())
        self.ever_committed = True
        self.turn += 1
        self._say_where(url)
        if sites.origin(url) not in self.home():
            self._uncover_once_drawn(self.turn)
            AppHelper.callLater(DRAWN_AT_THE_LATEST, self._uncover_if_still, self.turn)

    def _uncover_if_still(self, turn: int) -> None:
        # Not once the cover is up for the app's own page: that would show this one, and its address, until that one came.
        if turn == self.turn:
            self._uncover()

    def _uncover_once_drawn(self, turn: int) -> None:
        # At commit WebKit has nothing to draw yet, and uncovering then would show white until the page's stylesheets
        # arrive. The page's own paint timing says when something is really there.
        def answered(painted: Any, _error: Any) -> None:
            if self.closed or turn != self.turn or not self.covered:
                return
            if painted:
                self._uncover()
            else:
                AppHelper.callLater(DRAWN_CHECK, self._uncover_once_drawn, turn)

        if not self.closed:
            self.webview.evaluateJavaScript_completionHandler_("performance.getEntriesByType('paint').length > 0", answered)

    def finished(self) -> None:
        url = url_text(self.webview.URL())
        if sites.origin(url) not in self.home() or sites.sign_in_page_shows(url):
            self._uncover()

    def failed(self, error: Any) -> None:
        if error.code() in NOT_FAILURES or self.closed:
            return
        log.warning("A page in the sign-in sheet didn't load (%s): %s", error.code(), error.localizedDescription())
        # A page that's showing stays, as in a browser, unless the sheet was covered for the one that failed.
        if self.covered or not self.ever_committed:
            self._cover_with(f"This page didn't open. {error.localizedDescription()}", trouble=True)

    # ----------------------------------------------------------------- what a page may ask of a person
    def ask(self, frame: Any, words: Any, buttons: list[str], answer: Callable[[bool, str], None], text: Optional[str] = None) -> None:
        """Alert, confirm and prompt, over the sheet, saying which site is asking."""
        if self.closed:
            answer(False, "")
            return
        alert = AppKit.NSAlert.alloc().init()
        if frame is not None:
            host = str(frame.securityOrigin().host() or "") or self.name.stringValue()
            alert.setMessageText_(f"{host} says")
            alert.setInformativeText_(str(words))
        else:
            alert.setMessageText_(str(words))
        for title in buttons:
            alert.addButtonWithTitle_(title)
        field = None
        if text is not None:
            field = AppKit.NSTextField.alloc().initWithFrame_(((0, 0), (260, 24)))
            field.setStringValue_(text)
            alert.setAccessoryView_(field)

        def done(code: int) -> None:
            self.dialogs.remove(alert.window())
            answer(code == AppKit.NSAlertFirstButtonReturn, str(field.stringValue()) if field is not None else "")

        self.dialogs.append(alert.window())
        alert.beginSheetModalForWindow_completionHandler_(self.window, done)

    def choose_files(self, parameters: Any, answer: Callable[[Any], None]) -> None:
        panel = AppKit.NSOpenPanel.openPanel()
        panel.setAllowsMultipleSelection_(bool(parameters.allowsMultipleSelection()))
        panel.setCanChooseDirectories_(False)

        def done(code: int) -> None:
            self.dialogs.remove(panel)
            answer(panel.URLs() if code == AppKit.NSModalResponseOK else None)

        self.dialogs.append(panel)
        panel.beginSheetModalForWindow_completionHandler_(self.window, done)
