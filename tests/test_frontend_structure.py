"""Structural guards on the frontend that Python can actually check.

There is no JS test runner in this project, so these assert the wiring is
present rather than exercising it. They exist because each one encodes a bug
that already shipped once and would be silent if it came back.
"""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "curbcall_netlify_v4", "app.html")
SW = os.path.join(ROOT, "curbcall_netlify_v4", "sw.js")


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


class MapSizingTests(unittest.TestCase):
    """The map rendered blank intermittently: Leaflet built its tile grid
    while the container was still zero-height, requested nothing, and raised
    no error -- so the tile-retry path never heard about it. Timers alone are
    a guess about when layout finishes; the observer is the actual event."""

    def setUp(self):
        self.app = _read(APP)

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
        self.app = _read(APP)

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
        self.app = _read(APP)

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
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(here, "curbcall_netlify_v4", "app.html"),
                  encoding="utf-8") as fh:
            self.app = fh.read()

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
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(here, "curbcall_netlify_v4", "app.html"),
                  encoding="utf-8") as fh:
            self.app = fh.read()

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
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, os.pardir, "curbcall_netlify_v4",
                               "app.html"), encoding="utf-8") as f:
            self.src = f.read()

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
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, os.pardir, "curbcall_netlify_v4",
                               "app.html"), encoding="utf-8") as f:
            self.src = re.sub(r"//.*$", "", f.read(), flags=re.M)

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
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, os.pardir, "curbcall_netlify_v4",
                               "app.html"), encoding="utf-8") as f:
            self.src = f.read()

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
        with open(os.path.join(here, os.pardir, "curbcall_netlify_v4",
                               "app.html"), encoding="utf-8") as f:
            self.src = re.sub(r"//.*$", "", f.read(), flags=re.M)
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
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, os.pardir, "curbcall_netlify_v4",
                               "app.html"), encoding="utf-8") as f:
            self.src = f.read()

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
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, os.pardir, "curbcall_netlify_v4",
                               "app.html"), encoding="utf-8") as f:
            self.src = f.read()

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
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, os.pardir, "curbcall_netlify_v4",
                               "app.html"), encoding="utf-8") as f:
            self.src = re.sub(r"//.*$", "", f.read(), flags=re.M)

    def test_anything_added_counts_as_a_result(self):
        self.assertIn("if(total>0||added>0){", self.src)
        self.assertNotIn("if(total>0){", self.src)


class DestructiveActionIsNotFirstTests(unittest.TestCase):
    def setUp(self):
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, os.pardir, "curbcall_netlify_v4",
                               "app.html"), encoding="utf-8") as f:
            self.src = f.read()

    def test_export_comes_before_clear_all_bids(self):
        self.assertLess(self.src.index('id="export-feed-btn"'),
                        self.src.index('id="clear-feed"'))

    def test_clear_all_bids_still_confirms(self):
        i = self.src.index('getElementById("clear-feed").onclick')
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
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, os.pardir, "curbcall_netlify_v4",
                               "app.html"), encoding="utf-8") as f:
            self.src = f.read()

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
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, os.pardir, "curbcall_netlify_v4",
                               "app.html"), encoding="utf-8") as f:
            src = f.read()
        i = src.index("function renderAccount(){")
        body = src[i:i + 14000]
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
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, os.pardir, "curbcall_netlify_v4",
                               "app.html"), encoding="utf-8") as f:
            self.src = f.read()

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
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, os.pardir, "curbcall_netlify_v4",
                               "app.html"), encoding="utf-8") as f:
            self.src = re.sub(r"//.*$", "", f.read(), flags=re.M)

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
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, os.pardir, "curbcall_netlify_v4",
                               "app.html"), encoding="utf-8") as f:
            self.src = f.read()

    def test_switch_screen_closes_the_modal(self):
        i = self.src.index("function switchScreen(s){")
        head = self.src[i:i + 700]
        self.assertIn("closeModal()", head)
        self.assertLess(head.index("closeModal()"),
                        head.index('document.getElementById("screen-"+s)'))
