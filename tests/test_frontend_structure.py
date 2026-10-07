"""Structural guards on the frontend that Python can actually check.

There is no JS test runner in this project, so these assert the wiring is
present rather than exercising it. They exist because each one encodes a bug
that already shipped once and would be silent if it came back.
"""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB_DIR = os.path.join(ROOT, "curbcall_netlify_v4")
APP = os.path.join(WEB_DIR, "app.html")
APP_CSS = os.path.join(WEB_DIR, "styles.css")
APP_JS = os.path.join(WEB_DIR, "app.js")
SW = os.path.join(WEB_DIR, "sw.js")


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _read_app_all():
    """app.html's markup, styles.css and app.js concatenated, in that order.

    app.html used to hold all three inline; it was split out for
    maintainability (styles.css, app.js) with zero behaviour change. Tests
    whose lookups span more than one of the three -- a markup id near a CSS
    rule, a JS function that also touches an HTML attribute -- read the
    reassembled whole rather than being rewritten around the split.
    """
    return _read(APP) + "\n" + _read(APP_CSS) + "\n" + _read(APP_JS)


class MapSizingTests(unittest.TestCase):
    """The map rendered blank intermittently: Leaflet built its tile grid
    while the container was still zero-height, requested nothing, and raised
    no error -- so the tile-retry path never heard about it. Timers alone are
    a guess about when layout finishes; the observer is the actual event."""

    def setUp(self):
        self.app = _read(APP_JS)

    def test_map_settle_observes_the_container(self):
        body = self.app[self.app.index("function mapSettle("):]
        self.assertIn("observeMapSize", body[:400])

    def test_the_observer_ignores_a_zero_sized_container(self):
        """invalidateSize() on a hidden container just re-caches the same
        useless grid, and would consume the one event that mattered."""
        body = self.app[self.app.index("function observeMapSize("):]
        self.assertIn("if(!w||!h)return;", body[:900])

    def test_the_observer_is_attached_only_once_per_map(self):
        self.assertIn("_mapSizeObserved", self.app)

    def test_a_browser_without_resizeobserver_still_loads(self):
        body = self.app[self.app.index("function observeMapSize("):]
        self.assertIn('typeof ResizeObserver==="undefined"', body[:400])


class CompanyProfileSyncTests(unittest.TestCase):
    """Account fields synced through Supabase but the profile photo did not:
    avatar_url was added to the table and to the UI, and to neither the push
    nor the pull. It lived in localStorage on the device that uploaded it and
    appeared nowhere else. One shared field list is what stops that drifting
    again."""

    def setUp(self):
        self.app = _read(APP_JS)

    def test_push_and_pull_share_one_field_list(self):
        self.assertIn("const COMPANY_FIELDS=", self.app)

    def test_the_avatar_is_in_that_list(self):
        block = self.app[self.app.index("const COMPANY_FIELDS="):]
        self.assertIn("avatar_url", block[:200])

    def _fn(self, name):
        """The source of one function, to the start of the next one.

        Slicing at the first closing brace broke as soon as the function grew
        an early-return guard -- the test should follow the function, not its
        first statement."""
        start = self.app.index(f"async function {name}(")
        after = self.app.find("\nasync function ", start + 1)
        return self.app[start:after if after != -1 else start + 4000]

    def test_the_push_builds_its_row_from_the_list(self):
        self.assertIn("COMPANY_FIELDS", self._fn("pushCompanyProfile"))

    def test_a_failed_push_is_not_swallowed(self):
        """A profile that never reached the server looked identical to one
        that did -- which is what made an empty table so hard to diagnose."""
        body = self._fn("pushCompanyProfile")
        self.assertIn("toast(", body)
        self.assertNotIn("catch(e){}", body)

    def test_the_pull_does_not_gate_the_whole_profile_on_the_company_name(self):
        """Someone with a photo and a contact but no company name had their
        stored row ignored, then overwritten with the empty local copy."""
        body = self._fn("syncPullCompanyProfile")
        self.assertNotIn("data&&data.name", body)
        self.assertIn("COMPANY_FIELDS.some", body)


class DiagnosticsGatingTests(unittest.TestCase):
    """Diagnostics is admin-only. /health is unauthenticated by design, so the
    card must not be the thing that hands a contractor the server's scan
    history -- and its buttons must not exist for them either."""

    def setUp(self):
        self.app = _read(APP_JS)

    def test_the_card_only_renders_for_an_admin(self):
        self.assertIn("${isAdmin?renderDiagnostics():\"\"}", self.app)

    def test_the_health_check_only_runs_for_an_admin(self):
        self.assertIn("if(isAdmin)loadHealth();", self.app)

    def test_its_buttons_are_wired_defensively(self):
        """Unguarded getElementById on an absent card throws, which would
        abort the rest of renderAccount and leave support and sign-out dead."""
        self.assertIn("if(diagRefresh)", self.app)
        self.assertIn("if(diagCopy)", self.app)

    def test_the_admin_token_upgrades_the_health_request(self):
        self.assertIn('"X-Admin-Token":tok', self.app)


class ServiceWorkerTests(unittest.TestCase):
    def setUp(self):
        self.sw = _read(SW)
        self.app_dir = os.path.dirname(APP)

    def test_every_shell_file_actually_exists(self):
        """SHELL_FILES is installed with cache.addAll(), which is atomic: one
        404 throws away the entire shell cache. The call is wrapped in
        .catch(() => {}), so it fails silently and offline mode simply stops
        working. Deleting admin.html without updating this list did exactly
        that."""
        block = re.search(r"const SHELL_FILES = \[(.*?)\];", self.sw, re.S).group(1)
        files = re.findall(r'"([^"]+)"', block)
        self.assertTrue(files, "SHELL_FILES should not be empty")
        for f in files:
            self.assertTrue(os.path.exists(os.path.join(self.app_dir, f)),
                            f"{f} is cached by the service worker but does not exist")

    def test_shell_and_asset_caches_share_a_version(self):
        shell = re.search(r'SHELL_CACHE = "curbcall-shell-(v\d+)"', self.sw).group(1)
        asset = re.search(r'ASSET_CACHE = "curbcall-assets-(v\d+)"', self.sw).group(1)
        self.assertEqual(shell, asset)

    def test_the_deleted_admin_console_is_not_referenced(self):
        self.assertNotIn("admin.html", self.sw)


class FindRadiusDefaultTests(unittest.TestCase):
    """The default radius decides whether a new user sees a board or a blank.

    Benchmarked over eight metros on one day: 25 miles reads 88 towns and
    finds 2 bids, with SEVEN of eight metros returning nothing at all. 125
    miles reads 616 towns and finds 32, with none empty. It is arithmetic --
    a town lets about one job in this trade a year and 56% of municipal
    portals have nothing posted on a given day -- so a five-town radius
    cannot fill a board however well the engine reads it.

    Pinned because it is a one-character regression that would look like the
    scanner breaking.
    """

    def setUp(self):
        self.app = _read_app_all()

    def test_the_find_row_defaults_to_the_widest_radius(self):
        row = re.search(r'id="radius-row">(.*?)</div>\s*</div>', self.app, re.S)
        self.assertIsNotNone(row, "the Find radius row should be findable")
        active = re.findall(r'radius-btn active" data-r="(\d+)"', row.group(1))
        self.assertEqual(active, ["125"],
                         "exactly one button is active and it must be 125mi")

    def test_the_script_default_matches_the_highlighted_button(self):
        """A mismatch scans one radius while the UI claims another."""
        self.assertRegex(self.app, r"\blet radius=125;")

    def test_only_one_radius_button_is_ever_preselected(self):
        for row_id in ("radius-row", "up-radius-row", "leads-radius-row"):
            block = re.search(rf'id="{row_id}">(.*?)</div>\s*</div>',
                              self.app, re.S)
            if not block:
                continue
            active = re.findall(r'radius-btn active', block.group(1))
            self.assertEqual(len(active), 1, row_id)


class HiddenLeadsTabTests(unittest.TestCase):
    """Residential Leads is hidden behind a flag, not deleted.

    The permit feed covers three cities -- Austin TX, Cambridge MA and Baton
    Rouge LA -- so for every contractor currently on the outreach list the
    tab is permanently empty, and an always-empty tab costs more credibility
    than a missing one. Everything behind it stays wired so turning it back
    on is one line.
    """

    def setUp(self):
        self.app = _read_app_all()

    def test_the_flag_exists_and_is_off(self):
        self.assertRegex(self.app, r"const LEADS_ENABLED\s*=\s*false")

    def test_the_tab_is_removed_when_the_flag_is_off(self):
        self.assertIn("""querySelector('.nav-btn[data-s="leads"]')""", self.app)

    def test_the_screen_is_unreachable_even_by_a_stale_link(self):
        """A stored last-screen or an old deep link would otherwise land on a
        screen with no tab to leave it by."""
        self.assertIn('if(s==="leads"&&!LEADS_ENABLED)s="scan";', self.app)

    def test_the_feature_is_still_there_to_turn_back_on(self):
        for marker in ('id="screen-leads"', 'id="leads-btn"',
                       "function renderLeads", 'id="leads-list"'):
            self.assertIn(marker, self.app, marker)

    def test_nothing_runs_for_the_hidden_screen(self):
        """Building a Leaflet picker for a screen nobody can open is wasted
        work at every cold start."""
        self.assertIn("LEADS_ENABLED?createLocationPicker", self.app)
        self.assertIn('if(s==="leads"&&LEADS_ENABLED)renderLeads();', self.app)


if __name__ == "__main__":
    unittest.main()


class AuthScreenIsReachableTests(unittest.TestCase):
    """The sign-up card is taller than a phone screen and had nowhere to go.

    body is position:fixed;overflow:hidden, and #auth-screen had no overflow
    of its own, so anything past the fold was simply unreachable. It was
    centred as well, and a flex container centring content taller than itself
    overflows both ends -- the top of an overflowed flex line cannot be
    scrolled to at all. The logo above and the "Already have an account?"
    link below were clipped at the same time, on the one screen a new
    customer has to get through.
    """

    def setUp(self):
        self.src = _read(APP_CSS)

    def _rule(self, selector):
        """The declarations of one rule, comments removed.

        The comments in these rules quote the bad values they replaced, so
        matching raw text would find "justify-content:center" in a sentence
        explaining why it is gone.
        """
        i = self.src.index(selector + "{")
        body = self.src[i:self.src.index("}", i)]
        return re.sub(r"/\*.*?\*/", "", body, flags=re.S)

    def test_the_auth_screen_can_scroll(self):
        self.assertIn("overflow-y:auto", self._rule("#auth-screen"))

    def test_the_auth_screen_is_not_centre_justified(self):
        """Centring is what makes the top unreachable once it overflows."""
        self.assertNotIn("justify-content:center", self._rule("#auth-screen"))

    def test_the_auth_screen_clears_the_notch_and_home_indicator(self):
        rule = self._rule("#auth-screen")
        self.assertIn("env(safe-area-inset-top)", rule)
        self.assertIn("env(safe-area-inset-bottom)", rule)

    def test_the_app_shell_covers_the_whole_viewport(self):
        """100dvh left a band of page background below the nav once
        installed. A fixed element with inset:0 has no unit to resolve."""
        rule = self._rule("#app")
        self.assertIn("position:fixed", rule)
        self.assertIn("inset:0", rule)

    def test_the_shell_still_has_exactly_one_scroll_container(self):
        self.assertIn("flex:1;overflow-y:auto", self._rule(".screens"))


class PasswordRulesAgreeTests(unittest.TestCase):
    """One minimum, everywhere a password can be set.

    Signup enforces 8 and says so; changing it from the Account screen
    enforces 8. The reset-link flow enforced 6, which made a password reset
    the one way into the app to set a password weaker than the app otherwise
    allows -- and it said nothing about a minimum at all until it rejected
    you.
    """

    def setUp(self):
        self.src = re.sub(r"//.*$", "", _read(APP_JS), flags=re.M)

    def test_every_password_check_uses_the_same_minimum(self):
        found = set(re.findall(r"password[^;]{0,12}\.length<(\d+)", self.src))
        found |= set(re.findall(r"next[^;]{0,12}\.length<(\d+)", self.src))
        self.assertTrue(found, "no password length checks found")
        self.assertEqual(found, {"8"},
                         f"password minimums disagree: {sorted(found)}")

    def test_the_reset_screen_states_the_minimum_before_rejecting_you(self):
        i = self.src.index("function showPasswordReset()")
        card = self.src[i:i + 1400]
        self.assertIn("At least 8 characters", card)


class SignupNameIsTwoFieldsTests(unittest.TestCase):
    def setUp(self):
        self.src = _read_app_all()

    def test_first_and_last_name_are_separate_inputs(self):
        self.assertIn('id="auth-first"', self.src)
        self.assertIn('id="auth-last"', self.src)

    def test_the_old_single_field_is_gone(self):
        """Left behind, it would be a null getElementById in the capture."""
        self.assertNotIn('id="auth-name"', self.src)
        self.assertNotIn('"auth-name"', self.src)

    def test_the_browser_can_autofill_both(self):
        self.assertIn('autocomplete="given-name"', self.src)
        self.assertIn('autocomplete="family-name"', self.src)

    def test_they_are_stored_as_one_name(self):
        """Everything downstream wants a person's name, not two columns."""
        i = self.src.index("function capturePendingSignupName()")
        fn = self.src[i:i + 900]
        self.assertIn("filter(Boolean).join", fn)


class OnboardingFollowsTheAccountTests(unittest.TestCase):
    """It was marked done in localStorage only.

    On iOS the installed Home Screen app and Safari keep separate storage, so
    the same person answered the same two questions in each and reported that
    the app asks every time. A second device or a cleared cache did the same.
    """

    def setUp(self):
        here = os.path.dirname(os.path.abspath(__file__))
        self.src = re.sub(r"//.*$", "", _read(APP_JS), flags=re.M)
        with open(os.path.join(here, os.pardir,
                               "supabase_sync_schema.sql"), encoding="utf-8") as f:
            self.sql = f.read()

    def test_the_account_is_asked_before_anyone_is_interrupted(self):
        i = self.src.index("async function routeAfterAuth()")
        fn = self.src[i:i + 700]
        self.assertIn("accountHasOnboarded", fn)
        self.assertLess(fn.index("accountHasOnboarded"), fn.index("showOnboarding()"))

    def test_finishing_records_it_against_the_account(self):
        i = self.src.index("function finishOnboarding()")
        self.assertIn("pushOnboarded", self.src[i:i + 400])

    def test_a_slow_network_cannot_hang_the_app(self):
        i = self.src.index("async function accountHasOnboarded()")
        self.assertIn("Promise.race", self.src[i:i + 1200])

    def test_the_column_is_added_by_alter_not_create(self):
        """create table if not exists never alters an existing table."""
        self.assertIn("add column if not exists onboarded", self.sql)


class TapTargetsTests(unittest.TestCase):
    """A 15px-tall link is not tappable on a phone.

    Measured in a real browser at 390x844: "Sync now" was 59x15, the Company
    Info Edit button 318x27, Change Photo 27 tall, and each review star 30x30.
    44 is Apple's guidance and this file already used it for
    .empty-actions .btn-ghost, so these are brought in line rather than
    inventing a number.
    """

    def setUp(self):
        self.src = _read_app_all()

    def _near(self, needle, span=320):
        i = self.src.index(needle)
        return self.src[i:i + span]

    def test_sync_now_is_tappable(self):
        self.assertIn("min-height:44px", self._near('id="sync-now-link"'))

    def test_company_edit_is_tappable(self):
        self.assertIn("min-height:44px", self._near('id="co-edit-btn"'))

    def test_change_photo_is_tappable(self):
        self.assertIn("min-height:44px", self._near('for="avatar-input"'))

    def test_review_stars_are_tappable(self):
        rule = self._near(".stars button{")
        self.assertIn("min-height:44px", rule)
        self.assertIn("min-width:44px", rule)


class AvatarPlaceholderTests(unittest.TestCase):
    """It centred nothing, so an account with no photo showed a bare ring."""

    def setUp(self):
        self.src = _read(APP_JS)

    def test_the_placeholder_gets_an_initial(self):
        self.assertIn("function avatarInitial()", self.src)
        self.assertIn('<div class="avatar-placeholder">${esc(avatarInitial())}',
                      self.src)

    def test_a_broken_photo_falls_back_to_the_initial_too(self):
        i = self.src.index("onerror=\"this.replaceWith")
        self.assertIn("data-initial", self.src[i:i + 400])


class ScanResultAgreesWithTheListTests(unittest.TestCase):
    """The Find screen said "nothing open near you" while the Bids tab filled.

    total_bids is counted on the server; `added` is what mergeOpenBids
    actually put in the list. Two sources of truth for one question, and the
    failure mode reads as a broken app.
    """

    def setUp(self):
        self.src = re.sub(r"//.*$", "", _read(APP_JS), flags=re.M)

    def test_anything_added_counts_as_a_result(self):
        self.assertIn("if(total>0||added>0){", self.src)
        self.assertNotIn("if(total>0){", self.src)


class DestructiveActionIsNotFirstTests(unittest.TestCase):
    def setUp(self):
        self.src = _read_app_all()

    def test_export_comes_before_clear_all_bids(self):
        self.assertLess(self.src.index('id="export-feed-btn"'),
                        self.src.index('id="clear-feed"'))

    def test_clear_all_bids_still_confirms(self):
        i = self.src.index('byId("clear-feed").onclick')
        self.assertIn("confirm(", self.src[i:i + 300])


class ConnectionErrorsAreHonestTests(unittest.TestCase):
    """"Check your internet" was a guess, and usually the wrong one.

    Every one of these fires after the page itself has loaded, so the
    connection is demonstrably working. The real causes are the backend
    redeploying or restarting. Telling a contractor on a job site that their
    signal is bad sends them to reboot a router instead of tapping the button
    again ten seconds later.
    """

    def setUp(self):
        self.src = _read(APP_JS)

    def test_no_message_blames_the_users_connection_outright(self):
        code = re.sub(r"//.*$", "", self.src, flags=re.M)
        self.assertNotIn("Check your internet", code)

    def test_there_is_one_helper_and_it_asks_the_browser(self):
        i = self.src.index("function offlineOrServer()")
        fn = self.src[i:i + 500]
        self.assertIn("navigator.onLine", fn)
        self.assertIn("restarting", fn)

    def test_every_failed_request_uses_it(self):
        self.assertGreaterEqual(self.src.count("offlineOrServer()"), 4)


class BillingIsWhereYouAreSentTests(unittest.TestCase):
    """A scan refused for an inactive plan sends the customer to Account.

    The Billing card was fifteenth on that screen -- below Stats, Bid Alerts,
    Company Info, Support, Referrals, Reviews and Admin. So the app said
    "Check Account tab" and then buried the one thing it sent them for, at
    the exact moment they decide whether to pay.
    """

    def setUp(self):
        src = _read(APP_JS)
        i = src.index("function renderAccount(){")
        # To the next top-level function, not a fixed number of characters.
        # This was src[i:i + 14000], and renderAccount() is 24k: "Delete
        # account" sat at 14163, so the first addition anywhere above it
        # dropped the card out of the window and the test failed with
        # "substring not found" -- reporting a missing card rather than the
        # ordering it exists to check.
        end = src.find("\nfunction ", i + 1)
        body = src[i:end if end != -1 else len(src)]
        # Comments out. These notes explain the ordering by naming the very
        # cards being ordered ("below Stats, Alerts, Company Info, ..."), so
        # a raw index finds the sentence rather than the card.
        body = re.sub(r"<!--.*?-->", "", body, flags=re.S)
        self.body = re.sub(r"//.*$", "", body, flags=re.M)

    def _at(self, needle):
        return self.body.index(needle)

    def test_billing_comes_before_everything_else_on_the_screen(self):
        billing = self._at('id="status-card"')
        for later in ("Your Stats", "Bid Alerts", "Company Info",
                      "Support", "Refer a Contractor"):
            self.assertLess(billing, self._at(later),
                            f"Billing should come before {later!r}")

    def test_the_upgrade_offer_sits_with_it(self):
        self.assertLess(self._at('id="upgrade-section"'), self._at("Your Stats"))

    def test_destructive_account_actions_stay_at_the_bottom(self):
        """Delete account must not migrate up with this."""
        self.assertGreater(self._at("Delete account"), self._at("Your Stats"))

    def test_the_stripe_portal_link_does_not_pose_as_the_main_action(self):
        """For someone who never subscribed it is a dead end, and it sat
        above the pricing styled like a button."""
        i = self.body.index("Manage billing")
        link = self.body[max(0, i - 400):i + 60]
        self.assertIn("btn-quiet", link)
        self.assertIn("Already subscribed?", self.body)


class FormsAreLabelledTests(unittest.TestCase):
    """Found by walking the rendered page, not by reading the markup.

    The email field carried no autocomplete, so a phone would not offer the
    address the customer had already saved -- friction on every single
    sign-in. The three sort dropdowns announced themselves as nothing at all
    to a screen reader.
    """

    def setUp(self):
        self.src = _read(APP)

    def test_the_email_field_can_be_autofilled(self):
        i = self.src.index('id="auth-email"')
        self.assertIn('autocomplete="email"', self.src[i - 120:i + 200])

    def test_every_select_has_an_accessible_name(self):
        for tag in re.findall(r"<select[^>]*>", self.src):
            self.assertTrue("aria-label=" in tag or "aria-labelledby=" in tag,
                            f"unlabelled select: {tag}")


class ResultMessagesMatchTheScreenTests(unittest.TestCase):
    """Neither results screen may describe something the customer cannot see.

    Both had the same split: a count from the server decided the sentence,
    while the list underneath was built from a different field of the same
    response. When those disagree the app reports success over an empty
    panel, which reads as broken rather than quiet.
    """

    def setUp(self):
        self.src = re.sub(r"//.*$", "", _read(APP_JS), flags=re.M)

    def test_the_scan_counts_what_it_added(self):
        self.assertIn("if(total>0||added>0){", self.src)

    def test_upcoming_counts_what_is_on_the_tab(self):
        self.assertIn("const onTab=Object.values(upcomingData||{})", self.src)
        self.assertIn("status.textContent=onTab>0", self.src)

    def test_upcoming_no_longer_trusts_the_server_count_alone(self):
        self.assertNotIn("status.textContent=(d.total||0)>0", self.src)


class NavigationClosesAnyOpenSheetTests(unittest.TestCase):
    """A modal is 85% of the screen tall and covers the nav, so a person
    cannot navigate out from under one -- but code can, and a refused
    request calls goTo("account")."""

    def setUp(self):
        self.src = _read(APP_JS)

    def test_switch_screen_closes_the_modal(self):
        i = self.src.index("function switchScreen(s){")
        head = self.src[i:i + 700]
        self.assertIn("closeModal()", head)
        self.assertLess(head.index("closeModal()"),
                        head.index('document.getElementById("screen-"+s)'))


class TopLevelHandlersCannotCrashTheWholeScriptTests(unittest.TestCase):
    """document.getElementById(id).onclick=... used to throw immediately if
    id wasn't on the page -- and because every wiring statement like this
    runs once, at script-parse time, that throw didn't just skip the one
    handler, it killed the rest of the file outright, silently taking every
    handler wired after it down too. That already happened once, to the
    admin console, before it was removed (see ServiceWorkerTests above).
    byId() is the fix: the same lookup, but a missing id gets a shared
    stand-in instead of null, so wiring a handler onto it is a quiet no-op.
    Renaming or removing an element id from here on is at worst one dead
    button, never a dead app -- but only as long as nothing goes back to
    calling document.getElementById directly at the top level, which is
    exactly the regression this guards against.
    """

    def setUp(self):
        self.app = _read(APP_JS)

    def test_the_safe_lookup_helper_exists(self):
        i = self.app.index("function byId(id){")
        self.assertIn("||_NULL_EL", self.app[i:i + 120])

    def test_the_stand_in_tolerates_addeventlistener_too(self):
        """A plain {} has no addEventListener method -- the fallback must
        be more than an empty object, or the keydown-listener call sites
        would simply trade one crash for another."""
        i = self.app.index("_NULL_EL=")
        self.assertIn("addEventListener", self.app[i:i + 60])

    def test_no_top_level_statement_calls_getelementbyid_directly(self):
        """Every one of these must go through byId() instead. A bare
        document.getElementById( at column 0, followed by a handler
        assignment or addEventListener, is this exact bug shipping again."""
        offenders = re.findall(
            r'^document\.getElementById\([^\n]*?\.'
            r'(?:onclick|onchange|oninput|onsubmit|addEventListener)',
            self.app, re.M)
        self.assertEqual(offenders, [])


class DesktopLayoutTests(unittest.TestCase):
    """The signed-in app shell was phone-width and centred at every viewport
    -- deliberately, per --shell's own comment -- which meant a 1440px
    monitor got the same ~360px card floating alone in a sea of background
    that a phone gets, nav included. Confirmed by rendering the real page at
    1440x900 (see the session's screenshots): every list screen was one
    column deep no matter how wide the window was.

    These check the structural pieces of the fix hold together, not the
    rendered pixels -- real rendering was verified separately, with
    tests/test_web_app.js re-run afterward to confirm nothing at the actual
    320px test viewport changed (it is gated behind --bp-desktop, 960px, so
    it shouldn't).

    The nav itself was first shipped as an always-visible left sidebar, then
    changed again to a dropdown opened from a topbar trigger (mobile's
    bottom tab bar was never touched by either version) -- see
    NavDropdownTests below for that piece."""

    def setUp(self):
        self.css = _read(APP_CSS)

    def _desktop_rule(self):
        i = self.css.index("@media (min-width:960px){")
        depth = 0
        j = i + len("@media (min-width:960px){") - 1
        for k, ch in enumerate(self.css[i:], start=i):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return self.css[i:k + 1]
        self.fail("unterminated @media (min-width:960px) block")

    def test_the_breakpoint_variable_and_the_media_query_agree(self):
        """--bp-desktop documents the number; nothing actually reads the
        custom property as a breakpoint (CSS can't use a var() inside an
        @media condition), so the literal in the query is what truly governs
        this and the two must be kept equal by hand elsewhere."""
        self.assertIn("--bp-desktop:960px", self.css)
        self.assertIn("@media (min-width:960px)", self.css)

    def test_shell_widens_for_both_things_that_use_it(self):
        """--shell drives .screen's max-width and .topbar's centering inset
        (see the rules above this section) -- redefining it once here is
        what keeps both in step, rather than maintaining two numbers."""
        rule = self._desktop_rule()
        self.assertIn(":root{--shell:", rule)

    def test_the_four_list_screens_become_a_grid(self):
        rule = self._desktop_rule()
        for list_id in ("feed-list", "upcoming-list", "leads-list", "saved-list"):
            self.assertIn(f"#{list_id}", rule, list_id)
        self.assertIn("display:grid", rule)

    def test_the_section_header_and_empty_state_span_every_column(self):
        """Without this, .feed-label (and the "no matches" empty state)
        becomes a grid item too and lands squeezed into the first column
        instead of reading as a header above the cards."""
        rule = self._desktop_rule()
        self.assertIn(".feed-label", rule)
        self.assertIn(".empty", rule)
        self.assertIn("grid-column:1/-1", rule)

    def test_the_bottom_bar_underline_does_not_survive_into_the_dropdown(self):
        """.nav-btn.active::after draws a short underline centred beneath a
        stacked icon+label -- meaningless once they're rows in a dropdown
        list, and left alone it would show as a stray mark with nothing for
        it to underline."""
        rule = self._desktop_rule()
        self.assertIn(".nav-btn.active::after{display:none;}", rule)

    def test_auth_screen_is_not_touched_by_any_of_this(self):
        """The auth screen is a short form, not a list -- it was explicitly
        left out of this pass. #auth-screen must not appear inside the
        desktop rule at all."""
        rule = self._desktop_rule()
        self.assertNotIn("auth-screen", rule)
        self.assertNotIn("auth-card", rule)


class NavDropdownTests(unittest.TestCase):
    """The desktop sidebar from the first pass at this (see DesktopLayoutTests)
    was replaced by a dropdown: a .nav-trigger button in the topbar toggles
    the same #nav-menu/.nav-btn markup mobile already uses, instead of
    reserving a permanent 232px column for it. Mobile's bottom tab bar was
    never touched -- .nav-trigger stays display:none until the desktop
    breakpoint, so it can't be tapped (or even found) below it."""

    def setUp(self):
        self.app = _read(APP)
        self.css = _read(APP_CSS)
        self.js = _read(APP_JS)

    def _desktop_rule(self):
        i = self.css.index("@media (min-width:960px){")
        depth = 0
        for k, ch in enumerate(self.css[i:], start=i):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return self.css[i:k + 1]
        self.fail("unterminated @media (min-width:960px) block")

    def test_the_trigger_button_exists_in_the_topbar(self):
        i = self.app.index('<div class="topbar">')
        j = self.app.index('id="user-chip"', i)
        self.assertIn('id="nav-trigger"', self.app[i:j])

    def test_the_trigger_is_hidden_until_the_desktop_breakpoint(self):
        """Without this, the button would render (and be tappable) on
        mobile too, where there is no dropdown for it to open -- the bottom
        tab bar is the only nav mobile ever gets."""
        i = self.css.index(".nav-trigger{")
        self.assertIn("display:none", self.css[i:i + 50])

    def test_the_dropdown_shares_markup_with_the_bottom_tab_bar(self):
        """#nav-menu is the same element the id="nav-menu" attribute was
        added to on the existing bottom-nav div -- not a second, parallel
        nav that could drift out of sync with it."""
        i = self.app.index('class="bottom-nav"')
        self.assertIn('id="nav-menu"', self.app[i:i + 60])

    def test_the_dropdown_is_closed_by_default_on_desktop(self):
        rule = self._desktop_rule()
        i = rule.index("#nav-menu{")
        self.assertIn("display:none", rule[i:i + 60])

    def test_opening_the_dropdown_only_needs_a_body_class(self):
        """nav-open is toggled on <body> by app.js's trigger click handler
        -- asserting the CSS keys off it here guards against a rename on
        either side going unnoticed."""
        rule = self._desktop_rule()
        self.assertIn("body.nav-open #nav-menu{display:flex;}", rule)

    def test_clicking_a_tab_closes_the_dropdown(self):
        """Without this, picking Bids/Upcoming/etc. from the dropdown would
        leave it sitting open over the screen it just navigated to."""
        i = self.js.index('document.querySelectorAll(".nav-btn").forEach(b=>{')
        body = self.js[i:i + 200]
        self.assertIn("closeNavMenu()", body)

    def test_the_trigger_toggles_an_aria_expanded_state(self):
        """aria-expanded is how a screen reader learns the dropdown opened
        at all -- there's no visible focus change otherwise since the panel
        is positioned absolutely, outside the trigger's own box."""
        self.assertIn('aria-expanded="false"', self.app)
        i = self.js.index("navTrigger.onclick=")
        self.assertIn('setAttribute("aria-expanded"', self.js[i:i + 200])

    def test_an_outside_click_closes_the_dropdown(self):
        i = self.js.index('document.addEventListener("click",')
        body = self.js[i:i + 250]
        self.assertIn("closeNavMenu()", body)


class MapBasemapIsMutedNotSwappedTests(unittest.TestCase):
    """The raw OSM basemap is a bright cream rectangle that reads as a
    pasted-in widget against the app's dark shell. Switching the tile
    *provider* to a dark basemap was tried and reverted (see the comment at
    app.js's MAP_TILE_URL) because it washed out the pins -- the entire
    point of the map -- and sent street labels grey-on-grey. The fix instead
    filters only .leaflet-tile-pane, leaving markers/popups/controls, which
    live in separate Leaflet panes, completely untouched."""

    def setUp(self):
        self.css = _read(APP_CSS)
        self.app = _read(APP_JS)

    def test_only_the_tile_pane_is_filtered(self):
        self.assertIn(".leaflet-tile-pane{filter:", self.css)

    def test_the_tile_provider_itself_was_not_swapped(self):
        """Guards against re-introducing the already-reverted dark-tile-
        provider approach: the light OSM URL must still be the one in use."""
        self.assertIn("tile.openstreetmap.org", self.app)

    def test_pin_markers_are_not_inside_the_filtered_pane_rule(self):
        """A filter on a parent pane would dim anything placed inside it --
        make sure the rule targets the tile pane alone, not a shared
        ancestor that also holds markers or popups."""
        i = self.css.index(".leaflet-tile-pane{filter:")
        rule_end = self.css.index("}", i) + 1
        rule = self.css[i:rule_end]
        self.assertNotIn("marker", rule.lower())
        self.assertNotIn("popup", rule.lower())

    def test_the_map_background_matches_the_dark_shell_not_the_light_tiles(self):
        """#f2efe9 (the old light-card background) would show through as a
        mismatched seam while tiles are loading/panning now that the tile
        imagery itself is dimmed."""
        self.assertNotIn("#f2efe9", self.css)

    def test_desktop_map_height_grows_with_the_wider_card(self):
        """PR #26 widened the signed-in shell to ~1180px on desktop. Left at
        the phone-tuned clamp(180px,26vh,280px), the map became a thin
        letterbox strip instead of a map. The desktop media query must
        override the height for both map views."""
        i = self.css.index("@media (min-width:960px){")
        depth = 0
        j = i + len("@media (min-width:960px){") - 1
        rule = None
        for k, ch in enumerate(self.css[i:], start=i):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    rule = self.css[i:k + 1]
                    break
        self.assertIsNotNone(rule, "unterminated @media (min-width:960px) block")
        self.assertIn("#find-map-view", rule)
        self.assertIn("#leads-map-view", rule)
        self.assertIn("height:clamp(", rule)


class RemoveBidButtonTests(unittest.TestCase):
    """Dismissing a bid from the feed has to survive the next rescan of that
    city, since mergeOpenBids() replaces a rescanned city's whole list from
    the server -- a bid the server still calls open would otherwise just
    quietly come back. So "remove" is a persisted exclusion set (dismissed),
    checked at render time, not a splice out of bidData itself."""

    def setUp(self):
        self.js = _read(APP_JS)
        self.css = _read(APP_CSS)

    def test_dismissed_state_is_persisted_like_saved_and_notes(self):
        self.assertIn('store.get("dismissed"', self.js)

    def test_the_feed_filters_through_a_single_visibility_helper(self):
        """Three different places in renderFeed count/list a city's bids
        (the tile count, the running total, the row list) -- if even one of
        them still read raw bidData directly, a removed bid would vanish
        from the list but linger in the header count or a city tile."""
        i = self.js.index("function visibleBidsIn(")
        helper = self.js[i:i + 300]
        self.assertIn("isOpen(b)", helper)
        self.assertIn("!dismissed[", helper)
        body = self.js[self.js.index("function renderFeed("):self.js.index("function bidCard(")]
        self.assertNotIn(".filter(isOpen)", body,
            "renderFeed must route through visibleBidsIn(), not isOpen() directly, or a dismissed bid would still be counted/listed somewhere")
        self.assertEqual(body.count("visibleBidsIn("), 4, body.count("visibleBidsIn("))

    def test_the_remove_button_exists_on_every_card(self):
        i = self.js.index("function bidCard(")
        body = self.js[i:self.js.index("function attachBidEvents(")]
        self.assertIn('class="bid-remove"', body)
        self.assertIn("data-remove=", body)

    def test_clicking_remove_does_not_also_open_the_bid_detail(self):
        """The whole card is clickable to open detail -- without an explicit
        guard, a click that lands on the remove button would bubble up to
        the card's own handler and open the very bid just dismissed."""
        i = self.js.index("function attachBidEvents(")
        body = self.js[i:i + 400]
        self.assertIn('e.target.closest(".star,.bid-remove")', body)

    def test_remove_persists_so_a_rescan_cannot_bring_it_back(self):
        i = self.js.index("function removeBid(")
        body = self.js[i:i + 300]
        self.assertIn("dismissed[id]=true", body)
        self.assertIn('store.set("dismissed"', body)

    def test_the_button_defaults_to_visible_for_touch(self):
        """A touchscreen has no hover state, so a button that only appears
        on hover would never be reachable there at all -- it must default
        to visible and only be hidden-until-hover behind a media query that
        confirms the device genuinely has one."""
        i = self.css.index(".bid-remove{")
        base_rule = self.css[i:self.css.index("}", i) + 1]
        self.assertNotIn("opacity:0", base_rule)

    def test_hover_to_reveal_only_applies_on_real_pointing_devices(self):
        i = self.css.index("(hover:hover) and (pointer:fine)")
        block = self.css[i:i + 400]
        self.assertIn(".bid-remove{opacity:0", block)
        self.assertIn(".bid:hover .bid-remove", block)

    def test_keyboard_focus_still_reveals_it(self):
        """Hiding it until :hover would also hide it from a keyboard user
        tabbing through the list -- :focus-visible has to be in the same
        reveal rule as :hover, not left out."""
        i = self.css.index("(hover:hover) and (pointer:fine)")
        block = self.css[i:i + 400]
        self.assertIn(":focus-visible", block)


class DesktopScrollZoomTests(unittest.TestCase):
    """scrollWheelZoom is off by default -- deliberately, per MAP_OPTS's own
    comment, because on a phone the map sits mid-page in a single scrolling
    column and a wheel/trackpad scroll over it hijacked the page scroll
    instead. Desktop doesn't have that collision (real width to spare, a
    mouse can move off the map in any direction), and scroll-to-zoom is the
    expected way to zoom a map with a mouse there -- so mapOpts() turns it
    on above the desktop breakpoint instead of leaving one shared constant
    that can only pick one behavior for every viewport."""

    def setUp(self):
        self.app = _read(APP_JS)

    def test_the_shared_base_still_defaults_off(self):
        """MAP_OPTS itself must stay a safe phone-first default -- anything
        that reads it directly, now or later, should not get scroll-zoom by
        accident."""
        i = self.app.index("const MAP_OPTS=")
        self.assertIn("scrollWheelZoom:false", self.app[i:i + 200])

    def test_map_opts_helper_keys_scroll_zoom_to_the_desktop_breakpoint(self):
        i = self.app.index("function mapOpts(")
        body = self.app[i:i + 200]
        self.assertIn("scrollWheelZoom:window.innerWidth>=960", body)

    def test_both_maps_are_created_through_the_helper_not_the_raw_constant(self):
        """A map created with the bare MAP_OPTS constant would silently keep
        the phone-only default on every viewport."""
        self.assertEqual(self.app.count("L.map(mv,MAP_OPTS)"), 0)
        self.assertEqual(self.app.count("L.map(mv,mapOpts())"), 2)


class ElevationAndContrastTests(unittest.TestCase):
    """Every card sat on the page with only a 1px border -- nothing lifted
    off it, which is most of what read as flat/bleak in a dark UI. These
    check the shadow scale exists and actually reached each card-like
    component, and that .input and .btn-ghost -- previously the same thin
    bordered box, with no way to tell "type here" from "tap here" at a
    glance -- now read as opposite directions (recessed vs raised)."""

    def setUp(self):
        self.css = _read(APP_CSS)

    def test_the_shadow_scale_is_defined(self):
        i = self.css.index(":root{")
        root = self.css[i:self.css.index("}", i) + 1]
        for token in ("--shadow-sm:", "--shadow-md:", "--shadow-lg:", "--glow-accent:"):
            self.assertIn(token, root, token)

    def test_every_card_like_component_uses_the_shadow_scale(self):
        for selector in (".bid{", ".map-card{", ".account-card{", ".plan{"):
            with self.subTest(selector=selector):
                i = self.css.index(selector)
                rule = self.css[i:self.css.index("}", i) + 1]
                self.assertIn("box-shadow:var(--shadow", rule, selector)

    def test_the_featured_plan_gets_the_accent_glow_on_top_of_its_shadow(self):
        i = self.css.index(".plan.featured{")
        rule = self.css[i:self.css.index("}", i) + 1]
        self.assertIn("var(--glow-accent)", rule)

    def test_input_is_recessed_not_just_bordered(self):
        i = self.css.index(".input{width:100%")
        rule = self.css[i:self.css.index("}", i) + 1]
        self.assertIn("box-shadow:inset", rule)

    def test_ghost_button_is_raised_not_flat_like_an_input(self):
        """Before this, .btn-ghost's background was transparent -- on the
        page's own dark background that is visually identical to .input,
        which also sits on a dark background with just a border."""
        i = self.css.index(".btn-ghost{")
        rule = self.css[i:self.css.index("}", i) + 1]
        self.assertNotIn("background:transparent", rule)
        self.assertIn("box-shadow:var(--shadow", rule)

    def test_disabled_buttons_do_not_keep_a_raised_shadow(self):
        i = self.css.index(".btn-primary:disabled,.btn-ghost:disabled{")
        rule = self.css[i:self.css.index("}", i) + 1]
        self.assertIn("box-shadow:none", rule)


class EmptyStateAndPlanIconTests(unittest.TestCase):
    """.empty had CSS for an .icon div (sized for an emoji) that nothing in
    app.js ever filled in after emoji icons were replaced with the SVG
    sprite -- every empty state silently rendered as bare text. And the
    plan feature lists used a CSS ::before Unicode checkmark, a different
    visual language from the SVG icon set used on the same screen."""

    def setUp(self):
        self.css = _read(APP_CSS)
        self.js = _read(APP_JS)

    def test_empty_icon_css_targets_an_svg_not_a_bare_emoji_font_size(self):
        i = self.css.index(".empty .icon{")
        rule = self.css[i:self.css.index("}", i) + 1]
        self.assertNotIn("font-size", rule)
        i2 = self.css.index(".empty .icon .icon-svg{")
        self.assertIn("width:", self.css[i2:self.css.index("}", i2) + 1])

    def test_empty_html_helper_renders_an_icon(self):
        i = self.js.index("function emptyHTML(")
        body = self.js[i:self.js.index("\n", i)]
        self.assertIn('class="icon"', body)
        self.assertIn("icon-svg", body)
        self.assertIn("<use href=", body)

    def test_every_empty_state_call_site_uses_the_icon_helper(self):
        # The old hand-written pattern put <h3> right after the wrapper with
        # no icon div between them. Any surviving match means a call site
        # wasn't converted to emptyHTML() and still renders without an icon.
        self.assertNotIn('<div class="empty"><h3>', self.js)

    def test_plan_feature_lists_use_the_check_icon_not_a_unicode_glyph(self):
        self.assertNotIn("2713", self.css)
        i = self.js.index("function planLi(")
        body = self.js[i:self.js.index("\n", i)]
        self.assertIn("i-check", body)


class SearchScreenDesktopLayoutTests(unittest.TestCase):
    """Find, Upcoming and Leads each put a location form (and for Find/Leads,
    a map) at the very top of a 1180px-wide screen with nothing else beside
    it -- a "ZIP code or City, State" box the better part of a foot wide.
    .search-layout pairs the form with its map in a two-column grid at
    desktop width instead.

    The map sits in the *middle* of each form's fields on mobile (right
    after the primary button, before status/effort/save-search), so the
    markup splits the form into .search-controls (before the map) and
    .search-followup (after it) with the map card in between -- preserving
    that exact mobile reading order -- and grid-template-areas reassembles
    them into one column at desktop width instead of reordering the DOM."""

    def setUp(self):
        self.css = _read(APP_CSS)
        self.html = _read(APP)

    def _desktop_rule(self):
        i = self.css.index("@media (min-width:960px){")
        depth = 0
        for k, ch in enumerate(self.css[i:], start=i):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return self.css[i:k + 1]
        self.fail("unterminated @media (min-width:960px) block")

    def test_search_layout_places_controls_and_followup_in_one_column(self):
        rule = self._desktop_rule()
        i = rule.index(".search-layout{")
        grid = rule[i:rule.index("}", i) + 1]
        self.assertIn('"controls map"', grid)
        self.assertIn('"followup map"', grid)

    def test_find_screen_keeps_the_map_between_controls_and_followup_in_dom_order(self):
        # This is the exact regression almost shipped: wrapping "everything
        # except the map" in one .search-controls div moved the map to the
        # bottom of the mobile flow, after the save-search button, instead
        # of right after "Scan for Bids" where it was.
        screen = self.html[self.html.index('id="screen-scan"'):self.html.index('<!-- BIDS', self.html.index('id="screen-scan"'))]
        i_controls = screen.index('class="search-controls"')
        i_map = screen.index('id="find-map-card"')
        i_followup = screen.index('class="search-followup"')
        i_scan_btn = screen.index('id="scan-btn"')
        i_save_btn = screen.index('id="save-search-btn"')
        self.assertTrue(i_controls < i_scan_btn < i_map < i_followup < i_save_btn)

    def test_leads_screen_keeps_the_map_between_controls_and_followup_in_dom_order(self):
        screen = self.html[self.html.index('id="screen-leads"'):self.html.index('<!-- SAVED', self.html.index('id="screen-leads"'))]
        i_controls = screen.index('class="search-controls"')
        i_map = screen.index('id="leads-map-card"')
        i_followup = screen.index('class="search-followup"')
        i_leads_btn = screen.index('id="leads-btn"')
        i_status = screen.index('id="leads-status"')
        self.assertTrue(i_controls < i_leads_btn < i_map < i_followup < i_status)

    def test_upcoming_has_no_map_so_its_form_just_gets_capped_alone(self):
        # Upcoming has no map to pair with -- it should use the "alone"
        # variant directly, not get wrapped in a now-single-column grid.
        screen = self.html[self.html.index('id="screen-upcoming"'):self.html.index('<!-- RESIDENTIAL LEADS')]
        self.assertIn('class="search-controls alone"', screen)
        self.assertNotIn("search-layout", screen)


class ChromeShadowAndPricingGridTests(unittest.TestCase):
    """Every card got a shadow in the elevation pass, but the two bars that
    are on screen every single moment -- .topbar and .bottom-nav -- were
    left as a flat fill with a hairline border, so app chrome never looked
    like it sat above the content it frames. And the Account screen's two
    subscription plans were a single column stretched to the 1180px shell,
    rather than the usual side-by-side pricing-tier layout."""

    def setUp(self):
        self.css = _read(APP_CSS)
        self.js = _read(APP_JS)

    def test_topbar_has_a_downward_shadow_and_the_stacking_context_to_show_it(self):
        i = self.css.index(".topbar{")
        rule = self.css[i:self.css.index("}", i) + 1]
        self.assertIn("box-shadow:", rule)
        # .screens paints after .topbar in DOM order and would otherwise
        # hide the shadow bleeding into the area just below it -- this is
        # the fix, not incidental styling.
        self.assertIn("z-index:", rule)

    def test_bottom_nav_shadow_points_up_not_down(self):
        i = self.css.index(".bottom-nav{")
        rule = self.css[i:self.css.index("}", i) + 1]
        self.assertIn("box-shadow:0 -", rule)

    def _desktop_rule(self):
        i = self.css.index("@media (min-width:960px){")
        depth = 0
        for k, ch in enumerate(self.css[i:], start=i):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return self.css[i:k + 1]
        self.fail("unterminated @media (min-width:960px) block")

    def test_upgrade_section_is_a_two_column_grid_at_desktop(self):
        rule = self._desktop_rule()
        i = rule.index("#upgrade-section{")
        grid = rule[i:rule.index("}", i) + 1]
        self.assertIn("display:grid", grid)
        self.assertIn("1fr 1fr", grid)

    def test_license_key_section_spans_both_columns(self):
        i = self.js.index('<div class="license-key-section">')
        self.assertLess(i, self.js.index("Have a license key?"))
        rule = self._desktop_rule()
        self.assertIn(".license-key-section{grid-column:1/-1;}", rule)

    def test_plan_cards_stretch_to_equal_height_with_bottom_aligned_buttons(self):
        rule = self._desktop_rule()
        i = rule.index("#upgrade-section .plan{")
        self.assertIn("flex-direction:column", rule[i:rule.index("}", i) + 1])
        i2 = rule.index("#upgrade-section .plan ul{")
        self.assertIn("flex:1", rule[i2:rule.index("}", i2) + 1])


class InteractiveFeedbackTests(unittest.TestCase):
    """.bid already had hover/press feedback (box-shadow bump, scale-down on
    :active) -- every OTHER clickable control (.btn-primary, .btn-ghost,
    .radius-btn, .tile, .nav-btn, .user-chip, .star) had none at all, so
    only the bid list felt responsive and everything else on every screen
    felt inert. These check each control picked up both a :hover and an
    :active rule, matching that existing pattern."""

    def setUp(self):
        self.css = _read(APP_CSS)

    def _rule(self, selector):
        i = self.css.index(selector)
        return self.css[i:self.css.index("}", i) + 1]

    def test_every_control_has_hover_and_active_feedback(self):
        for selector in (
            ".btn-primary{", ".btn-ghost{", ".radius-btn{", ".tile{",
            ".nav-btn{", ".user-chip{", ".star{",
        ):
            with self.subTest(selector=selector):
                base = self._rule(selector)
                self.assertIn("transition:", base, selector)
                cls = selector[:-1]  # drop the trailing "{"
                self.assertIn(f"{cls}:hover{{", self.css, f"{cls} has no :hover rule")
                self.assertIn(f"{cls}:active{{", self.css, f"{cls} has no :active rule")

    def test_tile_active_selection_state_still_wins_over_hover(self):
        # .tile:hover and .tile.active have equal specificity -- .active
        # (the selected filter) must come after :hover in source order or
        # hovering a selected tile would wipe its amber "selected" colour.
        self.assertLess(self.css.index(".tile:hover{"), self.css.index(".tile.active{"))

    def test_nav_btn_active_tab_still_wins_over_hover(self):
        self.assertLess(self.css.index(".nav-btn:hover{"), self.css.index(".nav-btn.active{"))

    def test_radius_btn_selection_state_still_wins_over_hover(self):
        self.assertLess(self.css.index(".radius-btn:hover{"), self.css.index(".radius-btn.active{"))


class DismissedBidsCrossDeviceSyncTests(unittest.TestCase):
    """bids/upcoming/leads/lead_status all sync across a signed-in user's
    devices via the user_feeds table (pushFeeds()/syncPullFeeds()) -- but
    removeBid()'s "dismissed" exclusion set never joined that sync, and
    never even called queueFeedPush() to schedule one. A bid removed with
    the card's "x" on one device kept reappearing on every other device
    signed into the same account, because the feed itself resynced but
    which bids had been dismissed from it never did."""

    def setUp(self):
        self.js = _read(APP_JS)
        self.sql = _read(os.path.join(ROOT, "supabase_sync_schema.sql"))

    def test_remove_bid_schedules_a_sync_push(self):
        i = self.js.index("function removeBid(")
        body = self.js[i:self.js.index("\n}", i)]
        self.assertIn("queueFeedPush()", body)

    def test_push_feeds_uploads_dismissed(self):
        i = self.js.index("async function pushFeeds(")
        body = self.js[i:self.js.index("\n}", i)]
        self.assertIn("dismissed:dismissed", body)

    def test_sync_pull_adopts_dismissed_and_persists_it(self):
        i = self.js.index("async function syncPullFeeds(")
        body = self.js[i:self.js.index("\nfunction ", i)]
        self.assertIn("dismissed=data.dismissed", body)
        self.assertIn('store.set("dismissed",dismissed)', body)
        # Falsy-but-defined would be wrong here too, but the real failure
        # mode is a row written before this column existed: data.dismissed
        # is undefined there, not an empty dismissal, and must not wipe
        # whatever this device already had dismissed.
        self.assertIn("data.dismissed||dismissed", body)

    def test_bid_id_migration_rekeys_dismissed_too(self):
        """saved/pipeline/notes were already rekeyed when the id scheme
        changed from legacy truncated-text ids to the md5 hash; dismissed
        was added later and was missing from that rekey, so a bid dismissed
        under the old scheme could silently stop matching after migration."""
        i = self.js.index("function migrateBidIds(")
        body = self.js[i:self.js.index("\n}", i)]
        self.assertIn("dismissed=rekey(dismissed)", body)
        self.assertIn('store.set("dismissed",dismissed)', body)

    def test_schema_has_a_migration_for_the_dismissed_column(self):
        self.assertIn(
            "alter table user_feeds add column if not exists dismissed jsonb",
            self.sql,
        )


class ScanSummaryPanelTests(unittest.TestCase):
    """The Find screen's desktop layout left a dead block of space below the
    map once the form was capped to a sane width (see
    SearchScreenDesktopLayoutTests above). renderScanSummary() fills it with
    real state -- a running total already computed from bidData, the same
    source renderFeed() uses -- rather than inventing filler content, and
    hides itself entirely before a first scan."""

    def setUp(self):
        self.js = _read(APP_JS)
        self.html = _read(APP)
        self.css = _read(APP_CSS)

    def test_summary_element_exists_between_map_and_followup_in_dom_order(self):
        """Mobile has no grid -- DOM order IS visual order there, and this
        reads naturally right after the map, before the utility buttons."""
        screen = self.html[self.html.index('id="screen-scan"'):self.html.index('<!-- BIDS', self.html.index('id="screen-scan"'))]
        i_map = screen.index('id="find-map-card"')
        i_summary = screen.index('id="scan-summary"')
        i_followup = screen.index('class="search-followup"')
        self.assertTrue(i_map < i_summary < i_followup)

    def test_summary_hides_itself_when_there_is_nothing_to_show(self):
        i = self.js.index("function renderScanSummary(")
        body = self.js[i:self.js.index("\n}", i)]
        self.assertIn('el.style.display="none"', body)
        self.assertIn('el.innerHTML=""', body)

    def test_summary_uses_the_same_visibility_filter_as_the_bids_tab(self):
        """Must route through visibleBidsIn(), not raw bidData -- otherwise
        a dismissed or closed bid would still count toward the total shown
        here even though it does not appear on Bids itself."""
        i = self.js.index("function renderScanSummary(")
        body = self.js[i:self.js.index("\n}", i)]
        self.assertIn("visibleBidsIn(c)", body)

    def test_view_all_bids_button_navigates_to_the_bids_tab(self):
        i = self.js.index("function renderScanSummary(")
        body = self.js[i:self.js.index("\n}", i)]
        self.assertIn('goTo("feed")', body)

    def test_render_feed_refreshes_the_summary_unconditionally(self):
        """renderFeed() has two exit points (the empty-feed early return and
        the normal fall-through) -- the call must happen before either, or
        one of those paths would leave the Find-screen summary stale."""
        i = self.js.index("function renderFeed(){")
        first_lines = self.js[i:i + 300]
        self.assertIn("renderScanSummary();", first_lines)

    def test_switching_to_scan_refreshes_the_summary(self):
        """Every other screen refreshes its own content in switchScreen() on
        becoming active; Find must too, so it doesn't depend on showApp()'s
        initial renderFeed() call having already run."""
        i = self.js.index('document.getElementById("screen-"+s).classList.add("active")')
        body = self.js[i:i + 700]
        self.assertIn('if(s==="scan")renderScanSummary();', body)

    def test_grid_area_reserves_a_spot_for_the_summary_on_find_only(self):
        rule = self.css[self.css.index("#screen-scan .search-layout{"):]
        rule = rule[:rule.index("}") + 1]
        self.assertIn('"followup summary"', rule)


class RemainingFlatSurfacesTests(unittest.TestCase):
    """.auth-card, .modal, .toast, .filter-row input/select and .social-btn
    were the last surfaces in the app still completely flat after the
    elevation pass -- a sign-in card with no shadow at all (the first thing
    anyone ever sees), a bottom sheet and a toast that are supposed to float
    above other content but had nothing distinguishing them from it, and
    text inputs that didn't match the recessed look every other .input
    already had."""

    def setUp(self):
        self.css = _read(APP_CSS)

    def _rule(self, selector):
        i = self.css.index(selector)
        return self.css[i:self.css.index("}", i) + 1]

    def test_auth_card_has_a_shadow(self):
        self.assertIn("box-shadow:var(--shadow", self._rule(".auth-card{"))

    def test_modal_sheet_shadow_points_up_not_down(self):
        """The backdrop is behind the sheet, not above it -- a downward
        shadow would be invisible against the dimmed backdrop it's already
        sitting on, same reasoning as .bottom-nav's upward shadow."""
        self.assertIn("box-shadow:0 -", self._rule(".modal{"))

    def test_toast_has_a_shadow(self):
        """Toasts often float over another dark card, not the page
        background, so a shadow is the only thing marking them as above it
        at all."""
        self.assertIn("box-shadow:var(--shadow", self._rule(".toast{"))

    def test_filter_row_inputs_match_the_recessed_look_of_every_other_input(self):
        rule = self._rule(".filter-row input,.filter-row select{")
        self.assertIn("box-shadow:inset", rule)

    def test_social_btn_is_raised_like_every_other_button_and_responds_to_press(self):
        rule = self._rule(".social-btn{")
        self.assertIn("box-shadow:var(--shadow", rule)
        self.assertIn(".social-btn:hover{", self.css)
        self.assertIn(".social-btn:active{", self.css)


class MoreMissedButtonAndShadowGapsTests(unittest.TestCase):
    """.pchip (the bid-detail Submitted/Won/Lost/Passed toggle) and the
    .act-primary/.act-more/.modal-actions button rows (Save/Share/Calendar/
    Proposal, and every modal's Close/Unlock/Retry footer) were missed in
    the passes that gave every other button and toggle-pill in the app
    hover/press feedback and a shadow. Also covers the profile avatar
    (photo and initials fallback), the one remaining shadowless avatar."""

    def setUp(self):
        self.css = _read(APP_CSS)

    def _rule(self, selector):
        i = self.css.index(selector)
        return self.css[i:self.css.index("}", i) + 1]

    def test_pchip_has_hover_and_press_feedback(self):
        base = self._rule(".pchip{")
        self.assertIn("transition:", base)
        self.assertIn(".pchip:hover{", self.css)
        self.assertIn(".pchip:active{", self.css)

    def test_pchip_active_status_colour_still_wins_over_hover(self):
        self.assertLess(self.css.index(".pchip:hover{"), self.css.index(".pchip.active-submitted{"))

    def test_act_primary_and_act_more_buttons_have_shadow_and_feedback(self):
        rule = self._rule(".act-primary a,.act-primary button,.act-more button,.act-more a{")
        self.assertIn("box-shadow:var(--shadow", rule)
        self.assertIn("transition:", rule)
        self.assertIn(".act-primary a:hover,", self.css)
        self.assertIn(".act-primary a:active,", self.css)

    def test_act_primary_buttons_dont_override_ma_gold_background(self):
        """Regression: an earlier draft of this fix added background:var(
        --card) to the shared .act-primary/.act-more rule -- a class+element
        selector, which beats .ma-gold's single-class background and would
        have wiped out the gold "Call" button's accent colour everywhere
        it's used inside .act-primary."""
        rule = self._rule(".act-primary a,.act-primary button,.act-more button,.act-more a{")
        self.assertNotIn("background:", rule)

    def test_modal_actions_buttons_have_shadow_and_feedback(self):
        rule = self._rule(".modal-actions button,.modal-actions a{")
        self.assertIn("box-shadow:var(--shadow", rule)
        self.assertIn(".modal-actions button:hover,", self.css)
        self.assertIn(".modal-actions button:active,", self.css)

    def test_avatar_photo_and_placeholder_both_have_a_shadow(self):
        self.assertIn("box-shadow:var(--shadow", self._rule(".avatar-lg{"))
        self.assertIn("box-shadow:var(--shadow", self._rule(".avatar-placeholder{"))


class ShadowHueMatchesTheThemeTests(unittest.TestCase):
    """Every background in this theme (--bg, --surface, --card) leans the
    same blue-violet navy, not neutral grey. Shadows built from plain
    rgba(0,0,0,…) have no hue at all, so instead of receding into the page
    they sat on top of it looking like a mismatched grey smudge -- "badly
    blended," as reported. --shadow-rgb ties every shadow's colour to
    --bg's own r,g,b so a shadow reads as the page's own colour darkening,
    not a foreign overlay."""

    def setUp(self):
        self.css = _read(APP_CSS)

    def test_shadow_rgb_is_defined_from_bgs_actual_hue(self):
        i = self.css.index(":root{")
        root = self.css[i:self.css.index("}", i) + 1]
        self.assertIn("--bg:#0d0f18", root)
        # #0d0f18 == rgb(13,15,24) -- this must track --bg if it ever changes.
        self.assertIn("--shadow-rgb:13,15,24", root)

    def test_no_shadow_or_scrim_still_uses_a_hueless_black(self):
        """.spin is a loading-ring colour, not part of the elevation system,
        and is deliberately excluded -- everything else that casts a shadow
        or dims the screen (card shadows, modal scrim, inset inputs, the
        nav dropdown) must route through the tinted token."""
        before_spin = self.css[:self.css.index(".spin{")]
        # A literal numeric alpha (e.g. "rgba(0,0,0,0.5") only ever appears in
        # real CSS here -- the one explanatory comment that mentions
        # rgba(0,0,0,…) uses an ellipsis, not a digit, and must not trip this.
        self.assertIsNone(re.search(r"rgba\(0,0,0,\d", before_spin))

    def test_every_shadow_token_uses_the_tinted_variable(self):
        i = self.css.index(":root{")
        root = self.css[i:self.css.index("}", i) + 1]
        for token in ("--shadow-sm:", "--shadow-md:", "--shadow-lg:"):
            j = root.index(token)
            rule = root[j:root.index(";", j) + 1]
            self.assertIn("var(--shadow-rgb)", rule, token)
            self.assertNotIn("0,0,0", rule, token)


class DepthCuesBeyondAFlatShadowTests(unittest.TestCase):
    """A drop shadow alone reads as "this has a shadow," not "this is a
    raised surface" -- what sells actual depth is a highlight catching the
    top edge (as a physically raised object would show) plus a page
    background that isn't a flat, infinite-looking void behind every card."""

    def setUp(self):
        self.css = _read(APP_CSS)

    def test_every_shadow_level_has_a_top_inset_highlight(self):
        i = self.css.index(":root{")
        root = self.css[i:self.css.index("}", i) + 1]
        for token in ("--shadow-sm:", "--shadow-md:", "--shadow-lg:"):
            j = root.index(token)
            rule = root[j:root.index(";", j) + 1]
            self.assertIn("inset 0 1px 0 rgba(255,255,255,", rule, token)

    def test_highlight_is_on_top_not_bottom(self):
        """A highlight on the bottom edge would read as a groove (light
        catching a lip underneath), the opposite of a raised surface."""
        i = self.css.index(":root{")
        root = self.css[i:self.css.index("}", i) + 1]
        j = root.index("--shadow-md:")
        rule = root[j:root.index(";", j) + 1]
        # "inset 0 1px 0" -- zero horizontal offset, positive vertical
        # offset -- is the only direction that paints along the top inside
        # edge; a bottom highlight would need a negative vertical offset.
        self.assertIn("inset 0 1px 0 rgba(255,255,255,", rule)
        self.assertNotIn("inset 0 -1px 0", rule)

    def test_body_background_is_a_gradient_not_a_flat_fill(self):
        i = self.css.index("body{font-family:var(--ui);")
        rule = self.css[i:self.css.index("}", i) + 1]
        self.assertIn("radial-gradient(", rule)
        self.assertIn("var(--bg)", rule)


class PlanCardsDontTouchTheLinkAboveThemTests(unittest.TestCase):
    """.plan's own margin-bottom only ever spaced it from what comes AFTER
    it -- nothing put space before the first one, so the "Already
    subscribed? Manage billing" link and the plan cards sat flush against
    each other with zero gap (reported as "price cards touching the thing
    above it"). Pre-existing on mobile's single stacked column too; the
    desktop two-column grid just made it obvious, since both cards' top
    edges now touch that link in a single flat row."""

    def setUp(self):
        self.css = _read(APP_CSS)

    def test_upgrade_section_has_margin_above_it(self):
        # Shorthand "margin:TOP RIGHT BOTTOM" -- the first value is the top.
        i = self.css.index("#upgrade-section{margin:")
        rule = self.css[i:self.css.index("}", i) + 1]
        self.assertIn("margin:0.9rem ", rule)


class LicenseKeyBlockSpacingTests(unittest.TestCase):
    """#upgrade-section ends on the Activate Key button (margin above only)
    and the next .account-card ("Your Stats") has margin below only, so the
    two sat flush. The "Already paid?" hint also sat flush on the key
    input. Same seam shape as PlanCardsDontTouchTheLinkAboveThemTests."""

    def setUp(self):
        self.css = _read(APP_CSS)

    def test_upgrade_section_has_margin_on_both_ends(self):
        i = self.css.index("#upgrade-section{margin:")
        rule = self.css[i:self.css.index("}", i) + 1]
        self.assertIn("margin:0.9rem 0 1rem", rule)

    def test_key_input_is_spaced_from_the_hint_above_it(self):
        self.assertIn(".license-key-section .input{margin-top:", self.css)


class HomeScreenTests(unittest.TestCase):
    """Home is the opening screen. Exactly one screen and one nav button can
    start out active -- two of either shows two screens stacked, or a nav
    highlight on a tab you aren't on."""

    def setUp(self):
        self.html = _read(APP)
        self.app = _read(APP_JS)

    def test_home_is_the_only_screen_active_at_load(self):
        active = re.findall(r'class="screen active" id="([^"]+)"', self.html)
        self.assertEqual(active, ["screen-home"])

    def test_home_is_the_only_nav_button_active_at_load(self):
        active = re.findall(r'class="nav-btn active" data-s="([^"]+)"', self.html)
        self.assertEqual(active, ["home"])

    def test_home_and_leads_dont_share_an_icon(self):
        # Leads (residential permits) already uses the house.
        home = re.search(r'data-s="home"><svg class="icon-svg"><use href="#([^"]+)"', self.html).group(1)
        leads = re.search(r'data-s="leads"><svg class="icon-svg"><use href="#([^"]+)"', self.html).group(1)
        self.assertNotEqual(home, leads)
        self.assertIn(f'<symbol id="{home}"', self.html)

    def test_referral_copy_button_is_scoped_to_its_card(self):
        # Home and Account both render a referral card; a fixed id makes two
        # elements share it and getElementById binds only the first.
        i = self.app.index("async function loadReferralCard(")
        body = self.app[i:self.app.index("\n}\n", i)]
        self.assertNotIn('id="referral-copy-btn"', body)
        self.assertIn('el.querySelector(".referral-copy-btn")', body)

    def test_last_visit_is_read_once_per_session(self):
        # showApp re-runs on every token refresh; moving "since" to now on
        # each one would zero the "New bids" count an hour into a session.
        i = self.app.index("prevVisitAt=store.get(LAST_VISIT_KEY")
        self.assertIn("firstShow", self.app[i - 200:i])
