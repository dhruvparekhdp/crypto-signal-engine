"""
Phone layout. This dashboard is read on a phone more often than a desktop.

Two failures here are silent — they look fine in code review and only show up
on a real device — so both are pinned: CSS source order where selectors share
specificity, and tables that need their column names carried to the cells.
"""
import re
import unittest

import scheduler.health as health


class TestStylesheetOrder(unittest.TestCase):
    """
    The phone rules and the base rules share specificity, so source order
    decides. The phone block was originally inserted ABOVE the base styles and
    every override silently lost — the layout looked correct in the CSS and
    wrong on the device.
    """

    def setUp(self):
        self.css = health._HTML[:health._HTML.index("</style>")]

    def test_phone_rules_come_after_the_base_rules_they_override(self):
        self.assertGreater(self.css.index(".side-secondary{display:none}"),
                           self.css.index(".side-item{padding:7px"))

    def test_the_phone_block_is_last_in_the_stylesheet(self):
        tail = self.css[self.css.index("@media(max-width:640px)"):]
        self.assertNotIn(".side-item{padding:7px", tail)


class TestPhoneNavigation(unittest.TestCase):
    def setUp(self):
        self.html = health._HTML

    def test_ten_destinations_do_not_all_sit_on_the_bar(self):
        """A sideways scroller with no affordance hides half of them."""
        self.assertIn("side-secondary", self.html)
        self.assertIn(".side-secondary{display:none}", self.html)

    def test_the_five_kept_on_the_bar_are_the_live_ones(self):
        for tab in ("dashboard", "crypto", "paper", "guard", "accuracy"):
            with self.subTest(tab=tab):
                self.assertIn(f'class="side-item" data-tab="{tab}"', self.html)

    def test_the_rest_are_behind_more(self):
        for tab in ("historic", "watchlist", "diag", "settings"):
            with self.subTest(tab=tab):
                self.assertIn(f'class="side-item side-secondary" data-tab="{tab}"', self.html)

    def test_more_is_hidden_on_desktop(self):
        self.assertIn(".side-more{display:none}", self.html)

    def test_choosing_a_destination_closes_the_sheet(self):
        """Leaving it open over the screen you just opened is the classic bug."""
        self.assertIn("classList.remove('more-open')", self.html)

    def test_the_scrim_closes_it_too(self):
        self.assertIn('id="more-scrim" onclick="toggleMore()"', self.html)

    def test_the_bar_sits_at_the_bottom_not_the_top_left(self):
        """A top-left icon rail is the one place a thumb cannot reach."""
        block = self.html[self.html.index("@media(max-width:820px)"):]
        self.assertIn("bottom:0", block[:600])


class TestPhoneTables(unittest.TestCase):
    """
    A nine-column table in a horizontal scroller is technically readable and
    practically useless at 390px — you cannot see the symbol and the number at
    once. Rows become cards instead.
    """

    def setUp(self):
        self.html = health._HTML

    def test_rows_become_cards(self):
        self.assertIn(".tbl thead{display:none}", self.html)
        self.assertIn(".tbl,.tbl tbody,.tbl tr,.tbl td{display:block", self.html)

    def test_each_value_carries_its_column_name(self):
        self.assertIn("content:attr(data-label)", self.html)

    def test_labels_are_applied_without_touching_every_builder(self):
        """
        A dozen places build tables; one missed call is an unlabelled table on
        a phone with no other symptom, so an observer does it centrally.
        """
        self.assertIn("labelTables", self.html)
        self.assertIn("MutationObserver", self.html)

    def test_only_one_observer_walks_the_dom(self):
        """
        The helper moved into the shared snippet so the other four pages get
        it. The dashboard's own copy then became a second observer doing
        identical work on every mutation.
        """
        self.assertEqual(self.html.count("new MutationObserver"), 1)

    def test_the_table_class_is_actually_styled(self):
        """It was used by every new view and defined nowhere."""
        self.assertIn(".tbl{border-collapse:collapse", self.html)


class TestPhoneTouchAndText(unittest.TestCase):
    def setUp(self):
        self.html = health._HTML
        self.phone = self.html[self.html.index("@media(max-width:640px)"):]

    def test_inputs_are_sixteen_px_so_ios_does_not_zoom(self):
        self.assertIn(".cr-input{min-height:40px;font-size:16px}", self.phone)

    def test_nav_targets_are_large_enough_to_hit(self):
        self.assertIn("min-height:46px", self.phone)

    def test_the_kpi_tiles_are_a_grid_at_every_width(self):
        """They were referenced everywhere and never declared as one."""
        self.assertIn(".cards{display:grid", self.html)

    def test_the_footer_clears_the_fixed_bar(self):
        self.assertIn("footer{padding-bottom:76px}", self.phone)


class TestOtherPagesAreAlsoResponsive(unittest.TestCase):
    def test_data_and_settings_have_a_breakpoint(self):
        """Both are read on a phone as often as the dashboard and had none."""
        for name, page in (("data", health._DATA_HTML), ("settings", health._SETTINGS_HTML)):
            with self.subTest(page=name):
                self.assertIn("@media(max-width:640px)", page)
                self.assertIn("viewport", page)


class TestPaperTradingOnAPhone(unittest.TestCase):
    """
    The page that gets opened on a phone more than all the others together,
    and the last one still dragging an 820px table sideways.

    Its two tables are eleven and twelve columns. .pt-scroll existed to scroll
    them, which means you could see a position's market or its P&L but never
    both. The card treatment had been applied to /data, /predict and /audit
    and skipped here — exactly backwards, given which page is actually read
    on a phone.
    """

    def setUp(self):
        self.html = health._HTML
        self.phone = self.html[self.html.index("@media (max-width:640px){"):]

    def test_both_paper_tables_opt_into_cards(self):
        """Open positions and closed history, not just one of them."""
        self.assertIn("""el.innerHTML = '<table class="tbl">""", self.html)
        self.assertIn("""? '<table class="tbl">""", self.html)

    def test_no_paper_table_is_left_as_a_bare_table(self):
        """A bare <table> inside .pt-scroll is one that still scrolls."""
        import re

        window = self.html[self.html.index("function renderPaperPositions"):
                           self.html.index("function renderPaperSummary")]
        self.assertEqual(re.findall(r"<table>(?!.*tbl)", window), [])

    def test_the_eight_hundred_pixel_floor_is_lifted_on_a_phone(self):
        """
        Without this the card is 820px wide and scrolls exactly as before —
        the class alone does nothing while the width floor stands.
        """
        self.assertIn(".pt-scroll table{min-width:0}", self.phone)

    def test_the_scroller_itself_is_switched_off(self):
        self.assertIn(".pt-scroll{overflow-x:visible", self.phone)

    def test_the_market_cell_becomes_the_card_heading(self):
        """
        Labelling it "Market" and right-aligning it against nothing reads as
        a missing value rather than a title.
        """
        self.assertIn(".pt-scroll .tbl td:first-child{display:block", self.phone)
        self.assertIn(".pt-scroll .tbl td:first-child::before{content:none}", self.phone)

    def test_the_stop_target_rail_gets_the_full_width(self):
        """
        It is a drawing, not a number. In a label/value row it gets whatever
        is left over, which at this width is nothing.
        """
        self.assertIn(".pt-scroll .tbl td:has(.pt-prail){display:block", self.phone)
        self.assertIn(".pt-scroll .pt-prail{min-width:0", self.phone)


class TestColumnLabelsAreTheColumnName(unittest.TestCase):
    def test_a_sort_arrow_does_not_become_part_of_the_label(self):
        """
        Sortable headers carry their arrow in a child span. Reading
        textContent folds it in, and every card in the paper-trading history
        would be labelled "Closed↓" — with the arrow flipping as you sort.
        """
        snippet = health._THEME_SNIPPET
        self.assertIn("nodeType === 3", snippet)
        self.assertIn("own || th.textContent", snippet)

    def test_a_header_with_no_text_node_still_gets_a_label(self):
        """The fallback matters: an icon-only header would otherwise be blank."""
        self.assertIn("(own || th.textContent).trim()", health._THEME_SNIPPET)


class TestSettingsOnAPhone(unittest.TestCase):
    """
    /settings had a breakpoint that only collapsed two grids to one column.
    Everything else about it was still built for a mouse on a wide screen.
    """

    def setUp(self):
        page = health._SETTINGS_HTML
        self.phone = page[page.index("@media(max-width:640px)"):]

    def test_fields_are_sixteen_px_so_ios_does_not_zoom(self):
        """
        Under 16px Safari zooms the page in on focus and never zooms back.
        Every tap on a number left the operator pinching their way out.
        """
        self.assertIn(".form-group input,.form-group select{font-size:16px", self.phone)

    def test_the_topbar_wraps_instead_of_pushing_logout_off_screen(self):
        """
        Two links, a title and a right-aligned lock button in one unwrapping
        row. At 360px the button — the only way to log out — left the screen.
        """
        self.assertIn(".topbar{flex-wrap:wrap", self.phone)

    def test_the_container_keeps_its_bottom_clearance(self):
        """`padding:12px` replaced `padding-bottom:60px`, not just the sides."""
        self.assertNotIn(".container{padding:12px}", self.phone)
        self.assertIn("48px", self.phone)

    def test_meta_rows_stack_rather_than_collide(self):
        self.assertIn(".meta-row{flex-direction:column", self.phone)


class TestOnlyOneViewIsActive(unittest.TestCase):
    def test_exactly_one_tab_starts_active(self):
        """Two hardcoded actives stacked the dashboard and the signals page."""
        actives = re.findall(r'id="tab-[a-z]+" class="tab-content active"', health._HTML)
        self.assertEqual(len(actives), 1, actives)
        self.assertIn("tab-dashboard", actives[0])


class TestSignalCards(unittest.TestCase):
    """
    The card carries stop, entry and target with a track showing where price
    sits between them — the shape a trader already reads on the exchange.
    """

    def setUp(self):
        self.html = health._HTML

    def test_the_three_levels_are_shown_together(self):
        for label in ("Stop loss", "Entry", "Take profit"):
            with self.subTest(label=label):
                self.assertIn(f"<label>{label}</label>", self.html)

    def test_the_track_maps_stop_to_target(self):
        """Left-to-right reads the same for a long and a short."""
        self.assertIn("(p - sl) / (tp - sl) * 100", self.html)

    def test_expected_profit_is_leveraged(self):
        """A 0.34% move is 3.4% on margin at 10x — the number that matters."""
        self.assertIn("const roe = move * PAPER_LEVERAGE", self.html)
        self.assertIn("const PAPER_LEVERAGE =", self.html)

    def test_a_refused_setup_gets_no_action_button(self):
        """A live-looking Buy on a trade the bot refuses is a mixed message."""
        self.assertIn("viable ? (long?'Buy / Long':'Sell / Short') : 'Refused'", self.html)

    def test_a_sub_one_multiple_is_described_not_computed(self):
        """
        Below one round trip, "keeps N% of gross" goes negative — arithmetic
        run past the point it means anything.
        """
        self.assertIn("xcost <= 1", self.html)
        self.assertIn("costs more to open and close", self.html)

    def test_direction_colours_the_card(self):
        """
        The page was one flat slate; a good and a bad setup looked alike.
        Now expressed as palette variables so every theme reskins it.
        """
        self.assertIn(".sig.long{border-left-color:var(--pos-strong)", self.html)
        self.assertIn(".sig.short{border-left-color:var(--neg-strong)", self.html)

    def test_cards_go_two_up_when_there_is_room(self):
        self.assertIn("#cr-signals,#dash-signals{display:grid", self.html)
        self.assertIn("@media(max-width:900px){#cr-signals,#dash-signals", self.html)


class TestTheming(unittest.TestCase):
    """
    Themes previously reskinned only the older components through !important
    overrides, so switching left the sidebar and the signal cards blue while
    everything around them changed.
    """

    def setUp(self):
        self.html = health._HTML
        self.snippet = health._THEME_SNIPPET

    def test_the_new_components_carry_no_hardcoded_colour(self):
        block = self.html[self.html.index("/* ── Signal cards ─"):
                          self.html.index("</style>")]
        self.assertEqual(re.findall(r"#[0-9a-fA-F]{6}", block), [])

    def test_every_theme_defines_the_whole_variable_set(self):
        keys = ("--bg", "--panel", "--line", "--text", "--muted", "--accent")
        for theme in ("amber", "carbon", "crimson", "violet", "emerald", "light", "navy"):
            block = self.html[self.html.index(f'html[data-theme="{theme}"]'):]
            block = block[:block.index("}")]
            for k in keys:
                with self.subTest(theme=theme, key=k):
                    self.assertIn(k, block)

    def test_the_default_is_no_longer_blue(self):
        self.assertIn("||'amber'", self.snippet)

    def test_the_default_is_a_named_theme_so_overrides_apply(self):
        """
        With no data-theme the older components kept their original colours,
        because every override is scoped to html[data-theme].
        """
        self.assertNotIn("removeAttribute('data-theme')", self.snippet)

    def test_navy_survives_as_a_choice(self):
        self.assertIn('html[data-theme="navy"]', self.html)
        self.assertIn("setSiteTheme('navy')", self.html)

    def test_themes_are_switchable_without_leaving_the_page(self):
        self.assertIn('class="side-themes"', self.html)
        self.assertIn("setSiteTheme('crimson')", self.html)

    def test_the_active_theme_is_marked_once_the_dots_exist(self):
        """The bootstrap runs before the markup, so it waits for the DOM."""
        self.assertIn("DOMContentLoaded", self.snippet)


class TestNoDanglingElementReferences(unittest.TestCase):
    """
    The sidebar rewrite deleted the KPI tiles and the old nav, but the JS kept
    reaching for them. initView's FIRST statement touched grid-crypto, so it
    threw before switchTab or refresh could run — every panel stayed on its
    loading placeholder and the header read "Error — retrying". The page looked
    like a backend outage and was a null dereference.

    A missing element must never be able to blank the page, so this scans for
    the pattern that throws: a property read straight off a literal lookup.
    """

    def setUp(self):
        self.html = health._HTML
        self.ids = set(re.findall(r'id="([A-Za-z0-9_-]+)"', self.html))

    def test_every_direct_lookup_resolves_to_a_real_element(self):
        dangling = sorted({
            m.group(1)
            for m in re.finditer(r"getElementById\('([A-Za-z0-9_-]+)'\)\s*\.\s*\w+", self.html)
            if m.group(1) not in self.ids
        })
        self.assertEqual(dangling, [], f"JS reaches for missing elements: {dangling}")

    def test_the_removed_tiles_are_gone_from_both_sides(self):
        for gone in ("grid-crypto", "grid-sports", "tabbar-sports",
                     "nav-crypto", "nav-sports", "stat-uptime"):
            with self.subTest(el=gone):
                self.assertNotIn(f'id="{gone}"', self.html)
                self.assertNotIn(f"getElementById('{gone}').", self.html)

    def test_refresh_writes_through_a_null_safe_helper(self):
        """One absent tile should cost that tile, not the whole refresh."""
        self.assertIn("function setText(id, value)", self.html)
        block = self.html[self.html.index("async function refresh(){"):
                          self.html.index("switchTab((location.hash")]
        self.assertNotIn(".textContent=", block.replace("if(el) el.textContent", ""))


class TestEveryDestinationIsReachableOnAPhone(unittest.TestCase):
    """
    Six pages had no way in on a phone.

    The bar holds five items and a More button reveals the rest. The default
    `.side-more{display:none}` was declared *after* the phone rule that sets
    `display:flex`, at the same specificity — so the cascade resolved to none
    at every width, More never rendered, and Accuracy, Historic Data,
    Watchlist, Signal Audit, Diagnostics and Settings were unreachable.

    Nothing looked broken, which is why it survived: the bar rendered, five
    tabs worked, and the missing sixth control left no trace.
    """

    def setUp(self):
        from scheduler.health import _HTML
        self.html = _HTML

    def test_the_more_button_exists(self):
        self.assertIn("side-more", self.html)

    def test_the_phone_rule_is_declared_after_the_default(self):
        """Same specificity means source order decides; the phone rule must win."""
        default = self.html.index(".side-more{display:none}")
        phone = self.html.index(".side-more{display:flex}")
        self.assertLess(default, phone,
                        "the default must come first or it overrides the phone rule")

    def test_every_secondary_destination_is_in_the_sheet(self):
        for tab in ("accuracy", "historic", "watchlist", "audit", "diag", "settings"):
            with self.subTest(tab=tab):
                self.assertIn(f'data-tab="{tab}"', self.html)

    def test_a_hidden_tab_does_not_strand_the_refresh_label(self):
        """A label reading 'Paused' outlived the pause and looked like a fault."""
        self.assertNotIn("setText('refresh-label', 'Paused')", self.html)
        self.assertIn("pageshow", self.html)


class TestTheMoreSheetIsASheet(unittest.TestCase):
    """
    Opening More wrapped the bar into extra rows, which pushed the five
    primary tabs up the screen and reflowed the page behind them — the nav
    moving out from under the thumb at the moment it is being used. It now
    renders as a three-across grid of cards over the page.
    """

    def setUp(self):
        self.css = health._HTML[:health._HTML.index("</style>")]

    def test_the_secondary_items_lay_out_three_across(self):
        self.assertIn("flex:0 0 calc(33.333% - 3px)", self.css)

    def test_the_basis_accounts_for_its_own_margin(self):
        """Three at a flat 33.333% plus margins overflows and the third wraps."""
        self.assertNotIn("flex:0 0 33.333%;max-width:33.333%", self.css)

    def test_the_sheet_clears_the_home_indicator(self):
        self.assertIn("padding-bottom:calc(4px + env(safe-area-inset-bottom,0px))",
                      self.css)

    def test_it_reads_as_a_surface_in_front_of_the_page(self):
        self.assertIn(".sidebar.more-open{box-shadow:", self.css)


class TestPricesAreFormatted(unittest.TestCase):
    """
    Exit prices rendered as raw floats — 86807.23385676013, 1.5262954668640325
    — while entries beside them looked clean, which only meant the entry
    happened to divide evenly. fmtPrice already existed and already handled
    the three magnitudes; the paper tables simply never called it.
    """

    def setUp(self):
        self.html = health._HTML

    def test_every_price_cell_in_the_paper_tables_goes_through_the_formatter(self):
        window = self.html[self.html.index("function renderPaperPositions"):
                           self.html.index("function renderPaperSummary")]
        import re

        # Any ${...} holding a bare price field, not wrapped in a formatter.
        raw = re.findall(r"\$\{(?:t|p)\.(entry|exit|mark|liq|stop|target)\}", window)
        self.assertEqual(raw, [], f"unformatted price cells: {raw}")

    def test_the_formatter_keeps_small_and_large_prices_distinguishable(self):
        """A 1.526 XRP price and an 86,807 BTC price need different precision."""
        self.assertIn("if(p>=1000)", self.html)
        self.assertIn("return p.toFixed(6)", self.html)


class TestTheClosedTradeColumnsSayWhatTheyHold(unittest.TestCase):
    def test_the_running_balance_column_is_not_called_wallet(self):
        """
        wallet_after is `wallet_before + margin + net`: the margin returns on
        close while other open positions still hold theirs, so the figure is
        free cash at that moment, not a running balance. Two trades netting
        -0.30 and -0.50 showed 26.67 then 17.03, which reads as a bug until
        you know the column is measuring something else.
        """
        self.assertNotIn('<th class="r">Wallet</th>', health._HTML)
        self.assertIn('<th class="r">Free cash</th>', health._HTML)
