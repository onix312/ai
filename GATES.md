# Gates: NOZZA design v2 rollout

OWNS: site/**, connector/printflow/printing.py, connector/printflow/b2b.py, connector/printflow/config.py, connector/printflow/settings_schema.py, connector/tests/test_phase11.py, connector/tests/test_v5.py, connector/tests/test_print_group.py, connector/tests/test_settings_schema.py, connector/tests/test_materials_links.py, docs/nozza-design-v2-2026-10-03/**, IMPLEMENTATION-COVERAGE.md

Scope: Match the NOZZA v2 digital screens and 61 print layouts while preserving current behavior.

- [x] G1: Changed SPA assets pass syntax and panel integrity checks
  CHECK: node --check site/assets/core.js && node --check site/assets/app.js && node --check site/assets/bridge.js && node --check site/assets/ops.js && node --check site/assets/ops10.js && node --check site/assets/conveyor.js && node --check site/assets/money.js && node --check site/assets/clientbot.js && node --check site/assets/v19-shell.js && node --check site/assets/print.js && node scripts/panel-check.js && git diff --check
  EXPECT: Сценарии стенда: память слотов AMS — ok.
  EVIDENCE: automatic-evidence=v1; definition-sha256=dbadfda85cda9a6af59bb3b26eeb3a36e6f01e545a783ae6a07f7bac58090557; exit=0; EXPECT=matched; output-sha256=876ae8e64a4bdecbfe5bf55fd626de02b0fbf4f28b0240d95c73eb0e52bf0df6; output-bytes=7506; shell=C:\Windows\system32\cmd.exe; cwd=C:\Users\PC\.codex\worktrees\72f7\ai-main; path=9a62bcdd18c8/37 entries

- [ ] G2: Route and standalone reference checks are recorded in the coverage map
  PARTIAL: all 22 routes load in the visible browser; dashboard main geometry is within ±4 px at 1440; calculator, customers, pages, printers, queue, niches, client-bot, settings, library, conveyor, shift-center, and system-map have route-specific reference/offline passes; system-map header/core/grid align to screenshot geometry and 390 px has no horizontal overflow. Avito standalone tool now uses v2 tokens and preserves editor workflows, but no archive-specific screen reference is available; most route-by-route reference and state comparisons remain pending.
  EVIDENCE: see IMPLEMENTATION-COVERAGE.md

- [ ] G48: Dashboard period and primary controls stay in the page heading on mobile
  CHECK: python -m unittest connector.tests.test_v19_shell.PrintFlowV19ShellTests.test_dashboard_keeps_period_and_primary_actions_with_page_heading -v
  EXPECT: /test_dashboard_keeps_period_and_primary_actions_with_page_heading.*\.\.\. ok/
  EVIDENCE: python -m unittest connector.tests.test_v19_shell -q: 32/32 passed; node --check site/assets/app.js and site/sw.js passed; git diff --check passed; updated dashboard opened in embedded browser at #dashboard (desktop snapshot confirms periods and primary actions remain beside heading). 390×844 embedded browser screenshot confirms controls fit, KPI 3×2, no horizontal overflow; operator-focus heading and refresh fit the same row after CSS correction. Pixel-diff against reference and live backend states pending

- [ ] G49: Printer offline cards keep their surface and hierarchy on mobile
  CHECK: python -m unittest connector.tests.test_v19_shell.PrintFlowV19ShellTests.test_printer_park_respects_minimum_card_width_and_offline_readability -v
  EXPECT: /test_printer_park_respects_minimum_card_width_and_offline_readability.*\.\.\. ok/
  EVIDENCE: python -m unittest connector.tests.test_v19_shell -q: 32/32 passed; node --check site/assets/printer.js and git diff --check passed; embedded-browser 390×844 confirms three offline park cards have separate surfaces, error summary is not duplicated, selected printer commands remain unavailable, and six tabs scroll horizontally within their own rail.

- [ ] G50: Shelf KPI grid avoids an orphaned final card and follows the live reference hierarchy
  CHECK: python -m unittest connector.tests.test_v19_shell.PrintFlowV19ShellTests.test_shelf_actions_and_missing_code_state_are_explicit -v
  EXPECT: /test_shelf_actions_and_missing_code_state_are_explicit.*\.\.\. ok/
  EVIDENCE: Static contract now covers the offline grid class and live forecast-driven five-column placement. Embedded browser 1440 confirms offline KPI 3×2; 390×844 confirms the mobile 2×3 layout. Live summary/forecast arrangement is not runtime-verified because the preview has no backend connection.

- [ ] G3: Physical print layouts match JSON coordinates and atlas dimensions
  PARTIAL: JSON-driven mm preview connected; 61 docs / 962 blocks are in-bounds; browser verified A4 invoice and sticker imposition. Standalone print pages use reference geometry; the business-card/sign constructor now positions all blocks for `business-front`, `business-back`, `shelf-sign`, and `insert` from JSON and prints front/back on separate sheets. The A6 cafe flyer now maps `cafe-front`/`cafe-back` blocks from JSON, matches 105×148 mm, passes default-text fit checks and duplex sheet-order interaction in the embedded browser. The four `how-order` cards now use JSON geometry at 93.5×92 mm; the duration control updates the fourth card without text overflow. The 10-poster constructor's 103 blocks match A4 JSON within 0.00000062 mm / 0.00011 pt and A3-scaled JSON within 0.00034 mm / 0.00001 pt; A3 paper is exactly 297×420 mm. Wobbler's 6 blocks derive from `wobbler` JSON and atlas page 35 was visually compared; price-list's 38 blocks match JSON within 0.003 mm and atlas page 54 was visually compared; price-tags' six recipes load block geometry from JSON and were visually rendered against atlas pp. 12–17 in an isolated browser fixture; the standalone constructor renders 27 × 67×32 mm or 15 × 67×57 mm from `tag-clean`/`promo-offer` blocks. Sticker page now renders round and 50×25 mm JSON recipes including all six service labels; the nine selected templates have in-bounds blocks, the embedded browser verified switchable templates, generated QR SVG, and zero-count safeguards. The Ø30 production sticker now uses a circular JSON circle block and fixed `3D-печать` text; browser confirmed 30×30 mm, loaded logo and in-bounds blocks against atlas page 28. Atlas pages 21, 26–29 were rendered and reviewed. The box-label editor now maps its 92×52 mm card and logo/title/text to `box-label` JSON; browser verified QR SVG, selection/editing/add/duplicate/delete, and print blocking for long headings. Atlas page 31 reviewed; Enter/Space selection verified. QR scanning, 390 px viewport, and physical print pending. Atlas pages 42–53 rendered and page 42 visually compared. Remaining templates, other atlas pages, printer output, duplex, physical measurement and QR/Code128 scans are pending
  Promotion sheet now uses `promo-offer`/`promo-photo` block geometry (67×57 mm, 15 slots/A4); empty prices and missing photo are gated, overflowing offer text cannot print, and a short manual-price offer passed in an isolated localhost origin. Door signs now use `door-sign` JSON blocks at 186×86 mm and match atlas page 33 geometry; long title overflow is surfaced and excluded from print, and enabled cards retain generated QR SVGs. Atlas page 60, QR/Code128 scans, and physical print remain pending.
   Zone shelf signs now render `zone` JSON at 186×78 mm, matching atlas page 34 blocks within 0.004 mm; A4 mobile preview scrolls inside its container without widening the page, and keyboard selection works. Following the owner's report that the zone logo looked cut out, the padded RGB experiment was removed and the transparent source crop from the JSON recipe restored; an alpha test verifies transparent exterior and preserved light ribbon separator. Fresh embedded-browser verification of the correction is pending; other forms and the A4 price-list page still need fresh route-by-route visual passes.
  White and dark poster themes were checked in the embedded browser: light paper uses the transparent full-color logo, while the dark theme uses a separate white wordmark variant with colored ribbons. The shared full-color and monochrome print crops now preserve the selected logo's light separator between ribbon strokes and remove cream-matte contamination from antialiased edges without changing dimensions or alpha geometry. An isolated browser comparison checked both crops on white, lavender, violet, peach, and dark backgrounds. The sticker production page was freshly checked in the embedded browser at Ø40×40 and 50×25 mm: `sticker-nozza` keeps its reference accent/text without inserting an unlisted logo; `brand-rect` keeps the transparent logo plus generated QR. The Ø30 round recipe uses its exact 22×10 mm logo block over the JSON cream circle; the main and monochrome marks now retain their intended internal light separator. Physical print and QR scan checks remain pending.
  Business card face/back were freshly compared in the embedded browser with atlas pages 18–20; each card is 90×50 mm (340.156×188.969 CSS px) and its four blocks follow JSON in CSS-mm with zero overflow. Data now maps into its designated slogan/contact areas, three color modes remain selectable, and Telegram is persisted with contact fields. Empty-contact and QR placeholder states were reviewed; dummy contacts were cleared from the browser profile. The personal 85×55 card endpoint is implemented and checked in an isolated browser fixture; physical size and QR scan remain pending. Pixel-diff, shelf signs/insert, duplex, scan and physical print remain pending.
  Certificate and coupon production preview were rebuilt from the six/five JSON blocks and visually checked in the embedded browser against atlas pages 37–38. DOM cards resolve to 186×92 mm and 92×50 mm at 96 CSS px/in; fields align to the mm recipe, 2 certificates and up to 8 coupons remain on their respective A4 sheets. Demo amounts, numbers, expiry dates, offers, and policy text were removed. Real QR SVG is generated from the saved QR URL; enabled QR with an empty link disables printing, disabled QR removes its block, and a long promo code triggers the overflow guard. Physical print size and scanner verification remain pending.
  Production warranty printing now emits one true 148×210 mm A5 sheet; its 10 absolute blocks follow `warranty` JSON and use the selected transparent logo crop. Warranty months are stored through the settings schema (0–120, default 0 for a handwritten term); the client QR is generated only when both public URL and portal code exist, with explicit missing-configuration copy otherwise. Unit tests and an isolated embedded-browser preview cover exact DOM geometry, URI-encoded QR, logo loading, and the no-QR state. Physical print and QR scanning remain pending.
  EVIDENCE: not complete

- [ ] G4: Required viewport, keyboard, reduced-motion, error, empty, and live-data checks are evidenced
  PARTIAL: print overflow checked at 1280/1920/1024/390; calculator has 1440/390 responsive checks; printers, queue, and customers have 1280/1920/1024/390 responsive offline checks; specific routes have offline unknown-data states; zoom 200% and live backend states pending; queue has 720 px zoom emulation with no overflow and a visible Tab focus ring; summary menu keyboard toggle and reduced-motion transition suppression checked.
  EVIDENCE: see IMPLEMENTATION-COVERAGE.md

- [x] G5: Production 85×55 mm customer card follows the v2 JSON recipe and preserves safe customer/QR behavior
  CHECK: python -m unittest connector.tests.test_phase11.PrintTests.test_stickers_and_business_card -v
  EXPECT: /test_stickers_and_business_card.*\.\.\. ok/
  EVIDENCE: automatic-evidence=v1; definition-sha256=ea0cfd2dc879a76438d998b5d8d0bd6ddfe940a9e0493362eb6cae48972f4954; exit=0; EXPECT=matched; output-sha256=0b63f7a3bfd284a5f6e454a39f7b13622ad579be6e448472f757384625ddf6dc; output-bytes=216; shell=C:\Windows\system32\cmd.exe; cwd=C:\Users\PC\.codex\worktrees\72f7\ai-main; path=9a62bcdd18c8/37 entries

- [x] G6: Shared color and monochrome print logo crops retain the original light ribbon separator
  CHECK: python -m unittest connector.tests.test_materials_links.MaterialsStaticLinksTests.test_print_logo_keeps_light_ribbon_separator -v
  EXPECT: Ran 1 test
  EVIDENCE: automatic-evidence=v1; definition-sha256=42f88eccdbec0dc99a1c8c4a6c35bfe36624575f1eaad665b632ae37cabc9b40; exit=0; EXPECT=matched; output-sha256=e93f89aae5993a179b8da13f117f82510f9bfbe426ffed1ae2c99208ec04869b; output-bytes=473; shell=C:\Windows\system32\cmd.exe; cwd=C:\Users\PC\.codex\worktrees\72f7\ai-main; path=9a62bcdd18c8/37 entries

- [x] G7: Production warranty form follows the A5 JSON recipe and keeps warranty/QR settings valid
  CHECK: python -m unittest connector.tests.test_phase11.PrintTests.test_warranty_keeps_the_term_unknown_until_it_is_set connector.tests.test_phase11.PrintTests.test_warranty_ticket_carries_its_own_styles connector.tests.test_phase11.PrintTests.test_warranty_uses_real_encoded_qr_when_configured connector.tests.test_settings_schema.ValidateTests.test_warranty_months_can_be_saved_and_is_bounded -v
  EXPECT: Ran 4 tests
  EVIDENCE: automatic-evidence=v1; definition-sha256=b14dba3305908b1ecf9077dc760427a914b22e1d62eefe61c76ed2dbad8d9e1b; exit=0; EXPECT=matched; output-sha256=de6bb6712c9b676593dcf4575a0e32bbc611b90ba59b5690be12bbd6b6a26bcf; output-bytes=1033; shell=C:\Windows\system32\cmd.exe; cwd=C:\Users\PC\.codex\worktrees\72f7\ai-main; path=9a62bcdd18c8/37 entries

- [x] G8: B2B invoice, proposal, receipt, and waybill render from their v2 A4 recipes
  CHECK: python -m unittest connector.tests.test_phase11.PrintTests.test_b2b_forms_follow_v2_recipes connector.tests.test_phase11.PrintTests.test_b2b_documents_repeat_a4_recipe_for_long_orders connector.tests.test_v5.B2BTests connector.tests.test_print_group.B2BDocumentTests -v
  EXPECT: Ran 10 tests
  EVIDENCE: automatic-evidence=v1; definition-sha256=531a83119eb9a90cb341d80f59abe679aaecd7f8d700216de75670b4609f4f9b; exit=0; EXPECT=matched; output-sha256=d66cf2701941d3669ef6d443960ecfe0b73b70ace919f8b2cb4a23714548eb27; output-bytes=1457; shell=C:\Windows\system32\cmd.exe; cwd=C:\Users\PC\.codex\worktrees\72f7\ai-main; path=9a62bcdd18c8/37 entries

- [x] G9: Production workshop report renders all 44 blocks from the A4 JSON recipe
  CHECK: python -m unittest connector.tests.test_phase11.PrintTests.test_workshop_report_follows_v2_recipe -v
  EXPECT: Ran 1 test
  EVIDENCE: automatic-evidence=v1; definition-sha256=31139ad581966cb652878eb80bdae6439f3c1b6783d3827b600862d99dfc9b15; exit=0; EXPECT=matched; output-sha256=bc2034c836740bea48973be5ec918d4a855610bac7072b3f0041ccdda4b11d2d; output-bytes=230; shell=C:\Windows\system32\cmd.exe; cwd=C:\Users\PC\.codex\worktrees\72f7\ai-main; path=9a62bcdd18c8/37 entries

- [x] G10: Production pickup receipt renders the thermal JSON recipe and preserves all order lines
  CHECK: python -m unittest connector.tests.test_phase11.PrintTests.test_pickup_receipt_follows_thermal_recipe_and_keeps_all_lines -v
  EXPECT: Ran 1 test
  EVIDENCE: automatic-evidence=v1; definition-sha256=e32871a63072ca68ec3210f652eaa1e6b1e0f6247e8c17087628bde519948a90; exit=0; EXPECT=matched; output-sha256=55527318fbe60a2cb982f56d929f7ec8f71992055cc39ce634738c27909315af; output-bytes=278; shell=C:\Windows\system32\cmd.exe; cwd=C:\Users\PC\.codex\worktrees\72f7\ai-main; path=9a62bcdd18c8/37 entries

- [x] G11: Production spool thermal label renders all 3 recipe blocks
  CHECK: python -m unittest connector.tests.test_v9.WorkshopTests.test_spool_thermal_label_follows_v2_recipe -v
  EXPECT: Ran 1 test
  EVIDENCE: automatic-evidence=v1; definition-sha256=8547107ab483c2c28cd7c1a821658a925931390b5232be405b6a181867423322; exit=0; EXPECT=matched; output-sha256=3d36f345a623009db23e0ff02334ef191299e44079eaaa77e094be9f309447f8; output-bytes=236; shell=C:\Windows\system32\cmd.exe; cwd=C:\Users\PC\.codex\worktrees\72f7\ai-main; path=9a62bcdd18c8/37 entries

- [x] G12: Stock label gallery generates recipe-based 70×50 mm labels and a printable A4 imposition
  CHECK: python -m unittest connector.tests.test_materials_links.MaterialsStaticLinksTests.test_inventory_labels_use_physical_v2_recipes_for_a4_print -v
  EXPECT: Ran 1 test
  EVIDENCE: automatic-evidence=v1; definition-sha256=8c847296314b2a70b6afb53c94d6f2079f6537f97e1e05bf02fd8f940cffb2a2; exit=0; EXPECT=matched; output-sha256=9c424f2da65464cbe0040211903051af1ebe3338767c629114fe1d481c66fdde; output-bytes=293; shell=C:\Windows\system32\cmd.exe; cwd=C:\Users\PC\.codex\worktrees\72f7\ai-main; path=9a62bcdd18c8/37 entries

- [x] G13: Production pack sheet renders all 25 A4 recipe blocks and retains long orders
  CHECK: python -m unittest connector.tests.test_phase11.PrintTests.test_pack_sheet_follows_recipe_and_repeats_for_long_orders -v
  EXPECT: Ran 1 test
  EVIDENCE: automatic-evidence=v1; definition-sha256=dfa2aed575c5952c691adfc2e06fb36a4f3df18c2b7192ea194489c551c3ef59; exit=0; EXPECT=matched; output-sha256=4ed1ab192718e548244e0c445cfa0855789e2208c353c9d2c5edda2c2e508cb2; output-bytes=270; shell=C:\Windows\system32\cmd.exe; cwd=C:\Users\PC\.codex\worktrees\72f7\ai-main; path=9a62bcdd18c8/37 entries

- [x] G14: Seller memo and application forms use their A4 JSON recipes
  CHECK: python -m unittest connector.tests.test_materials_links.MaterialsStaticLinksTests.test_seller_documents_use_their_a4_recipes -v
  EXPECT: Ran 1 test
  EVIDENCE: automatic-evidence=v1; definition-sha256=72ae2af9cc25ee1391210cb38caa20b06913e9f82ea583e16e1c81cf01087c61; exit=0; EXPECT=matched; output-sha256=392d3b6e6b55ed5ac932b5446c6dcc2263ed177b3d40e6d4f6e6c3fb431f67cf; output-bytes=261; shell=C:\Windows\system32\cmd.exe; cwd=C:\Users\PC\.codex\worktrees\72f7\ai-main; path=9a62bcdd18c8/37 entries

- [x] G15: Zone sign logo uses a transparent crop with a preserved light separator
  CHECK: python -m unittest connector.tests.test_materials_links.MaterialsStaticLinksTests.test_zone_sign_logo_uses_transparent_print_crop -v
  EXPECT: Ran 1 test
  EVIDENCE: PASS: crop 910×326; outside alpha=0; inner separator alpha=255 and RGB channels >=220; full materials test module passes. Browser visual confirmation pending.

- [x] G22: Box-label editor uses the v2 screen surface while retaining the white 92×52 mm print card
  CHECK: python -m unittest connector.tests.test_materials_links.MaterialsStaticLinksTests.test_box_label_editor_uses_v2_screen_shell_and_preserves_print_card -v
  EXPECT: Ran 1 test
  EVIDENCE: static contract confirms shared v2 shell, screen-scoped dark editor, physical card size, JSON block rendering, and existing editor/print actions; browser visual check pending.

- [x] G23: Production sticker generator uses the v2 screen shell while retaining print recipes and actions
  CHECK: python -m unittest connector.tests.test_materials_links.MaterialsStaticLinksTests.test_production_stickers_use_v2_screen_shell_and_keep_print_recipes -v
  EXPECT: Ran 1 test
  EVIDENCE: static contract confirms v2 screen stylesheet, screen-scoped editor overrides, JSON-sized labels, print styles, and existing quantity/QR/template controls; browser visual check pending.

- [x] G24: Wobbler editor uses the v2 screen shell while retaining its JSON-sized atlas card
  CHECK: python -m unittest connector.tests.test_materials_links.MaterialsStaticLinksTests.test_wobbler_editor_uses_v2_screen_shell_and_preserves_atlas_card -v
  EXPECT: Ran 1 test
  EVIDENCE: static contract confirms v2 screen stylesheet, screen-scoped editor overrides, JSON block rendering, 92×87 mm card, print overflow behavior, and existing actions; browser visual check pending.

- [x] G25: How-to-order tool gets a v2 screen shell without styling over its JSON print cards
  CHECK: python -m unittest connector.tests.test_materials_links.MaterialsStaticLinksTests.test_how_order_screen_shell_preserves_json_cards_and_duration_field -v
  EXPECT: Ran 1 test
  EVIDENCE: static contract confirms screen-only dark shell, white 93.5×92 mm cards, JSON recipe source, retained duration field, and print hiding; browser visual check pending.

- [x] G16: Production sign constructor exposes shop and shelf JSON layouts without removing legacy sizes
  CHECK: python -m unittest connector.tests.test_materials_links.MaterialsStaticLinksTests.test_sign_constructor_maps_shop_and_shelf_sizes_to_v2_recipes -v
  EXPECT: Ran 1 test
  EVIDENCE: PASS: Ran 1 test; door-sign 186×86, shop-sign 92×65, and shelf-sign 90×55 have three in-bounds blocks; static page links resolve. Inline JavaScript syntax check passed.

- [x] G17: Telegram content plan uses v2 utility styling and retains plan-editing actions
  CHECK: python -m unittest connector.tests.test_materials_links.MaterialsStaticLinksTests.test_telegram_content_plan_uses_v2_tool_surface_and_keeps_actions -v
  EXPECT: Ran 1 test
  EVIDENCE: PASS: Ran 1 test; v2 utility styling, responsive/reduced-motion rules and existing copy/export/edit/reset/localStorage actions are present. Inline JavaScript syntax, HTTP 200 and static material links passed. Browser-panel open queued; rendered visual comparison remains pending.

- [x] G18: Social-header generator uses v2 digital shell while retaining original image sizes and downloads
  CHECK: python -m unittest connector.tests.test_materials_links.MaterialsStaticLinksTests.test_social_header_generator_uses_v2_shell_without_changing_png_exports -v
  EXPECT: Ran 1 test
  EVIDENCE: PASS: Ran 1 test; all three original canvas dimensions, PNG download, drawing functions and contact storage remain; static links and inline JavaScript syntax pass. Preview endpoint returned HTTP 200; rendered screenshot comparison pending.

- [x] G19: About/prompt generator uses v2 tool styling and retains all edit/copy/download behavior
  CHECK: python -m unittest connector.tests.test_materials_links.MaterialsStaticLinksTests.test_about_and_ai_prompt_uses_v2_tool_surface_and_keeps_outputs -v
  EXPECT: Ran 1 test
  EVIDENCE: PASS: Ran 1 test; all 11 fields and prompt/about/download actions are present. Inline JavaScript syntax and static links pass; preview endpoint returned HTTP 200. Rendered screenshot comparison pending.

- [x] G20: Certificate/coupon editor uses the v2 screen shell while keeping print artwork white and exact-size
  CHECK: python -m unittest connector.tests.test_materials_links.MaterialsStaticLinksTests.test_certificate_coupon_editor_uses_v2_screen_shell_and_white_print_cards -v
  EXPECT: Ran 1 test
  EVIDENCE: PASS: Ran 1 test; screen-only dark styling and white print cards are scoped separately, JSON recipe/Qr/count/overflow/print guards remain. Inline JavaScript syntax and static links pass; preview endpoint returned HTTP 200.

- [x] G21: Promotion-stand editor uses v2 screen surfaces while keeping JSON recipe cards white
  CHECK: python -m unittest connector.tests.test_materials_links.MaterialsStaticLinksTests.test_promo_stand_editor_uses_v2_screen_shell_and_white_recipe_cards -v
  EXPECT: Ran 1 test
  EVIDENCE: PASS: Ran 1 test; v2 screen shell is scoped to screen media and `stand.v2` remains white; offer/photo recipe selectors, 15-card actions and print guard remain. Inline JavaScript syntax and static links pass; preview endpoint returned HTTP 200.

- [x] G27: Settings are pinned above connector status and participate in navigation search
  CHECK: python -m unittest connector.tests.test_v19_shell.PrintFlowV19ShellTests.test_settings_is_pinned_above_connection_and_remains_searchable -v
  EXPECT: Ran 1 test
  EVIDENCE: static contract confirms one pinned settings link before the status chip, removal from the collapsible tools group, and query filtering/empty-state accounting; browser check pending.

- [x] G28: Dashboard priority order follows NOZZA v2 specification
  CHECK: python -m unittest connector.tests.test_v19_shell.PrintFlowV19ShellTests.test_dashboard_follows_design_spec_priority_order -v
  EXPECT: Ran 1 test
  EVIDENCE: HTML order is KPI → urgent actions/deadlines → live printer/briefing → orders/finance; wide layout uses an 8:4 split and removes the oversized 616px minimum. Embedded browser render remains unconfirmed.

- [x] G29: Shared application shell uses v2 geometry
  CHECK: python -m unittest connector.tests.test_v19_shell.PrintFlowV19ShellTests.test_shared_shell_dimensions_match_design_spec -v
  EXPECT: Ran 1 test
  EVIDENCE: Header is 64px; NOZZA mark 34px; sidebar search and navigation targets 40px. Embedded browser geometry comparison remains pending.

- [x] G30: Dashboard typography and density follow the v2 scale
  CHECK: python -m unittest connector.tests.test_v19_shell.PrintFlowV19ShellTests.test_dashboard_typography_and_density_match_design_spec -v
  EXPECT: Ran 1 test
  EVIDENCE: Dashboard uses 28/36 h1, 18/26 h2, 14/20 table/body controls, 28/34 KPI and 20px card padding. Embedded screenshot comparison remains pending.

- [x] G31: NOZZA v2 stylesheet is available in the offline application shell
  CHECK: python -m unittest connector.tests.test_v19_shell.PrintFlowV19ShellTests.test_shell_assets_are_in_offline_cache -v
  EXPECT: Ran 1 test
  EVIDENCE: Service worker cache is version 115 and includes `/assets/nozza-design-v2.css`.

- [x] G32: Shared typography and control scale is applied across internal SPA routes
  CHECK: python -m unittest connector.tests.test_v19_shell.PrintFlowV19ShellTests.test_spa_uses_shared_v2_typography_tokens -v
  EXPECT: Ran 1 test
  EVIDENCE: Internal SPA uses the documented h1/h2, table, KPI and control scales; styling is scoped to `body.pf-v19 .view` inside screen media.

- [x] G33: Compact SPA controls keep 44px touch targets on narrow screens
  CHECK: python -m unittest connector.tests.test_v19_shell.PrintFlowV19ShellTests.test_spa_uses_shared_v2_typography_tokens -v
  EXPECT: Ran 1 test
  EVIDENCE: At widths up to 760px, primary/compact buttons, tabs, fields and sidebar links receive 44px minimum targets.

- [x] G34: SPA navigation uses the documented drawer breakpoint through 1279px
  CHECK: python -m unittest connector.tests.test_v19_shell.PrintFlowV19ShellTests.test_shared_shell_dimensions_match_design_spec -v
  EXPECT: Ran 1 test
  EVIDENCE: At 1279px and below the sidebar slides off canvas, the burger opens it, and main content no longer reserves a sidebar column; at 1280px the persistent desktop sidebar remains.

- [x] G35: Narrow topbar retains access to status and secondary controls without overflow pressure
  CHECK: python -m unittest connector.tests.test_v19_shell.PrintFlowV19ShellTests.test_compact_header_moves_secondary_controls_into_existing_menu -v
  EXPECT: Ran 1 test
  EVIDENCE: Existing live printer, sound, density and theme controls move into the accessible More menu at 460px and restore at wider sizes; browser overflow verification remains pending.

- [x] G36: Orders screen keeps v2 reading scale and kanban geometry
  CHECK: python -m unittest connector.tests.test_v19_shell.PrintFlowV19ShellTests.test_orders_page_keeps_documented_scale_and_kanban_geometry -v
  EXPECT: Ran 1 test
  EVIDENCE: Fast views use readable labels/counts, kanban columns remain 280–320px, order names are 16px, and narrow layouts reflow controls; embedded render comparison pending.

- [x] G37: Printer park cards follow the 280px minimum and offline copy is readable
  CHECK: python -m unittest connector.tests.test_v19_shell.PrintFlowV19ShellTests.test_printer_park_respects_minimum_card_width_and_offline_readability -v
  EXPECT: Ran 1 test
  EVIDENCE: CSS contract requires 280px minimum park cards, 13/19px explanatory text, 40px tabs and a one-column narrow workspace; rendered verification pending.

- [x] G38: Product and filament stock KPIs follow v2 reference scale
  CHECK: python -m unittest connector.tests.test_v19_shell.PrintFlowV19ShellTests.test_stock_kpis_and_product_cards_follow_reference_scale -v
  EXPECT: Ran 1 test
  EVIDENCE: Desktop KPI grid is 5+1 at 1440 reference width with 104px cards and 28px values; tablet/mobile grids and 280px product tile minimum are encoded. Updated browser render pending.

- [x] G39: Shelf missing-code warning and quick stock actions state their effect
  CHECK: python -m unittest connector.tests.test_v19_shell.PrintFlowV19ShellTests.test_shelf_actions_and_missing_code_state_are_explicit -v
  EXPECT: Ran 1 test
  EVIDENCE: Missing 1C code receives a dedicated warning class; quick sell and stock receipt labels explain the action without changing the existing handler contract. Render comparison pending.

- [x] G40: Warehouse empty and offline states occupy the full warehouse grid
  CHECK: python -m unittest connector.tests.test_v19_shell.PrintFlowV19ShellTests.test_warehouse_empty_state_spans_grid_and_period_controls_are_readable -v
  EXPECT: Ran 1 test
  EVIDENCE: Successful-empty and offline warehouse states span all grid columns; batch and ledger period segments have 40px controls. Updated rendered comparison pending.

- [x] G41: Documents table scrolls locally and row click stays read-only
  CHECK: python -m unittest connector.tests.test_v19_shell.PrintFlowV19ShellTests.test_documents_table_keeps_local_scroll_and_click_only_opens -v
  EXPECT: Ran 1 test
  EVIDENCE: Data rows retain all nine columns behind an internal horizontal scroller; offline/empty state avoids forced table width. Row click calls openDoc and has no posting request.

- [x] G42: Finance colors ordinary amounts neutrally and reserves semantic colors for results/risks
  CHECK: python -m unittest connector.tests.test_v19_shell.PrintFlowV19ShellTests.test_finance_neutral_amounts_do_not_use_positive_green -v
  EXPECT: Ran 1 test
  EVIDENCE: Revenue and shelf sales no longer turn green solely because they are positive; available funds only signal a negative balance, while existing result/risk coloring remains intact.

- [x] G43: Calculator visual rules target the current input/result card wrappers
  CHECK: python -m unittest connector.tests.test_v19_shell.PrintFlowV19ShellTests.test_calculator_card_polish_targets_wrapped_sections_and_stacks_on_mobile -v
  EXPECT: Ran 1 test
  EVIDENCE: Input and result cards receive the card edge, shadow and heading scale after grouping; result cards stack to one column at 700px and below.

- [x] G44: Niche hypotheses distinguish an empty funnel from views with no leads
  CHECK: python -m unittest connector.tests.test_v19_shell.PrintFlowV19ShellTests.test_niche_verdict_distinguishes_no_data_from_views_without_leads -v
  EXPECT: Ran 1 test
  EVIDENCE: A truly empty funnel keeps the no-data text; recorded views with zero leads now receive a warning describing that measurable state, instead of claiming there is no data.

- [x] G45: Customer aftercare reports offline uncertainty accessibly
  CHECK: python -m unittest connector.tests.test_v19_shell.PrintFlowV19ShellTests.test_customer_aftercare_offline_state_is_explicit_and_accessible -v
  EXPECT: Ran 1 test
  EVIDENCE: Offline queue state is announced as unknown (`role=status`) without a duplicate error toast; online failures keep their alert and toast path.

- [x] G46: Background finance report does not toast expected connector outage
  CHECK: python -m unittest connector.tests.test_v19_shell.PrintFlowV19ShellTests.test_finance_report_offline_state_is_explicit_without_global_toast -v
  EXPECT: Ran 1 test
  EVIDENCE: An expected offline report failure clears report values to an accessible unknown state; genuine errors while online still raise the existing toast.

- [x] G47: Document row action exposes its destination to assistive technology
  CHECK: python -m unittest connector.tests.test_v19_shell.PrintFlowV19ShellTests.test_documents_table_keeps_local_scroll_and_click_only_opens -v
  EXPECT: Ran 1 test
  EVIDENCE: The arrow button announces the document number it opens; its click handler remains read-only and calls `openDoc` only.

- [x] G51: Warehouse summary keeps all four KPIs together on desktop
  CHECK: python -m unittest connector.tests.test_v19_shell.PrintFlowV19ShellTests.test_warehouse_empty_state_spans_grid_and_period_controls_are_readable -v
  EXPECT: Ran 1 test
  EVIDENCE: Built-in browser checked at 1440×900 and 390×844; desktop shows four KPI in one row and mobile shows 2×2. Warehouse data remain unknown while backend is offline.

- [x] G52: Filament inventory summary matches the four-card reference
  CHECK: python -m unittest connector.tests.test_v19_shell.PrintFlowV19ShellTests.test_stock_kpis_and_product_cards_follow_reference_scale -v
  EXPECT: Ran 1 test
  EVIDENCE: Built-in browser verified at 1440×900 and 390×844; four summary metrics preserve spool count in the mass card sublabel; live spool records remain unavailable offline.

- [x] G53: Compact mobile topbar truncates long page names without colliding with controls
  CHECK: python -m unittest connector.tests.test_v19_shell.PrintFlowV19ShellTests.test_shared_shell_dimensions_match_design_spec -v
  EXPECT: Ran 1 test
  EVIDENCE: Embedded browser at 390×844 shows the batch title and subtitle ellipsized within the reserved title width, with status buttons unobstructed.

- [x] G54: Finance offline notices fill the intended cards without false values
  CHECK: python -m unittest connector.tests.test_v19_shell.PrintFlowV19ShellTests.test_finance_report_offline_state_is_explicit_without_global_toast -v
  EXPECT: Ran 1 test
  EVIDENCE: Built-in browser desktop screenshot shows the unavailable-data notices vertically centered in the two finance cards; at 390×844 the cash cards stack in one column and KPI still show unknown values as dashes.

- [x] G55: Finance tabs hide inactive cash content when P&L or reports are selected
  CHECK: python -m unittest connector.tests.test_v19_shell.PrintFlowV19ShellTests.test_finance_report_offline_state_is_explicit_without_global_toast -v
  EXPECT: Ran 1 test
  EVIDENCE: Built-in browser confirmed Cash → P&L → Reports transitions; each screenshot shows only the selected pane, with offline values remaining unknown.

- [x] G56: Embedded AI active chat uses a flat dark surface instead of a full-panel workshop illustration
  CHECK: python -m unittest connector.tests.test_v19_shell.PrintFlowV19ShellTests.test_embedded_ai_rail_keeps_illustration_out_of_active_chat_surface -v
  EXPECT: Ran 1 test
  EVIDENCE: Built-in browser screenshot at 1440×900 shows the rail without duplicated/full-height character artwork; greeting and controls remain visible.

- [x] G57: Dashboard starts with the production console and shift briefing before live KPIs and urgent work
  CHECK: python -m unittest connector.tests.test_v19_shell.PrintFlowV19ShellTests.test_dashboard_follows_reference_composition_order -v
  EXPECT: Ran 1 test
  EVIDENCE: Embedded browser at 1440×900 and 1024×768 shows heading → workshop/briefing → six offline KPI → urgent actions/deadlines. At 1024 the workshop and briefing share the first row; offline state remains explicit. 390 DOM order confirmed; exact mobile screenshot geometry pending.

- [x] G58: Printer telemetry bars are hidden while the station connection and readings are unknown
  CHECK: python -m unittest connector.tests.test_v19_shell.PrintFlowV19ShellTests.test_printer_park_respects_minimum_card_width_and_offline_readability -v
  EXPECT: Ran 1 test
  EVIDENCE: Embedded browser at 1440×900, 1024×768, and 390×844 shows dashed unknown readings without empty bars/sparklines. At 1024 telemetry stacks within the right column; at 390 cards stack and tabs scroll within their strip. Fan labels and dashes fit their card.

- [x] G59: Filament inventory KPI grid and metric labels match the reference composition
  CHECK: python -m unittest connector.tests.test_v19_shell.PrintFlowV19ShellTests.test_stock_kpis_and_product_cards_follow_reference_scale -v
  EXPECT: Ran 1 test
  EVIDENCE: Embedded browser at 1440×900 shows four KPI cards in one row; 390×844 shows 2×2. Specification's five metrics remain represented: spool count is a sublabel of total grams. Removed conflicting legacy six/three-card base grid; offline labels remain unknown.

- [x] G60: Queue controls and work/history panels remain usable across desktop, tablet, and phone
  CHECK: python -m unittest connector.tests.test_v19_shell.PrintFlowV19ShellTests.test_printers_and_queue_visual_contract -v
  EXPECT: Ran 1 test
  EVIDENCE: Embedded browser at 1440×900, 1024×768, and 390×844 confirms four summary cards, search/mode/group controls, and separate work/history panels. At 390 actions remain touch-sized, KPI uses 2×2, toolbar wraps locally, and both panels stack; offline counters remain unknown and start controls disabled.

- [x] G61: Calculator mobile layout places the result after all primary inputs
  CHECK: python -m unittest connector.tests.test_v19_shell.PrintFlowV19ShellTests.test_growth_and_calculator_visual_contract -v
  EXPECT: Ran 1 test
  EVIDENCE: Embedded browser at 390×844 confirms the input groups precede batch cost/price and advanced scenarios; “What if” remains collapsed. Offline price stays unknown (`—`) and the API error is explicit.

- [x] G62: Calculator's unknown recommended price is separated from its caption
  CHECK: python -m unittest connector.tests.test_v19_shell.PrintFlowV19ShellTests.test_growth_and_calculator_visual_contract -v
  EXPECT: Ran 1 test
  EVIDENCE: At 1440×900 the offline dash is centered on its own line, with “recommended price per item” centered below it. Calculation remains unavailable until the connector returns data.

- [x] G63: Niche summary and unavailable hypotheses remain clear on desktop and mobile
  CHECK: python -m unittest connector.tests.test_v19_shell.PrintFlowV19ShellTests.test_niche_verdict_distinguishes_no_data_from_views_without_leads -v
  EXPECT: Ran 1 test
  EVIDENCE: Embedded browser at desktop shows instruction → four KPI → unavailable hypotheses; 390×844 collapses KPI to one column. Unknown metrics remain dashes and the failed catalog is not represented as a successful empty result.

- [x] G64: Client-bot work surfaces precede settings and retain explicit offline states
  CHECK: python -m unittest connector.tests.test_v19_shell.PrintFlowV19ShellTests.test_clientbot_surfaces_inbox_before_collapsible_settings -v
  EXPECT: Ran 1 test
  EVIDENCE: Built-in browser desktop/mobile shows unread inbox and payment reconciliation before a collapsed settings disclosure. Offline KPI are unknown; inbox/payments say unavailable, static commands remain visible, payment table scrolls locally, and no duplicate offline toast covers the page. Token remains password-masked and was not read.

- [x] G65: Dashboard empty/offline orders state fits its card without an empty horizontal table scroller
  CHECK: python -m unittest connector.tests.test_v19_shell.PrintFlowV19ShellTests.test_dashboard_offline_orders_do_not_force_wide_empty_table -v
  EXPECT: Ran 1 test
  EVIDENCE: Embedded browser at 1280 CSS px reports card width 651 px and table-wrap clientWidth=scrollWidth=609 px; screenshot confirms offline notice spans the card with no table header or horizontal scrollbar. The CSS condition retains the 740 px table only when real order rows exist; backend/live rows remain unverified.

- [x] G66: Settings search and save controls align with the desktop reference while remaining usable on mobile
  CHECK: python -m unittest connector.tests.test_v19_shell.PrintFlowV19ShellTests.test_settings_header_actions_match_reference_alignment -v
  EXPECT: Ran 1 test
  EVIDENCE: Embedded browser at 1280 CSS px places search/reset on the first action row and Save below search. At 390×844 the title/actions fit within the page, category strip scrolls locally, and document width remains 380 px; offline settings remain disabled.





- [x] G67: Products desktop workspace frame aligns to the 1352×885 reference viewport
  CHECK: python -m unittest connector.tests.test_v19_shell.PrintFlowV19ShellTests.test_products_workspace_matches_reference_frame_at_1352px -v
  EXPECT: Ran 1 test
  EVIDENCE: Embedded browser screenshot at 1352×885: content x=252, title y=92, subnavigation y=187, KPI y=259, filter toolbar y=519. This confirms major geometry only; pixel-diff and live data remain pending.

- [x] G68: Offline printer progress does not resemble a completed or zero-percent job
  CHECK: python -m unittest connector.tests.test_v19_shell.PrintFlowV19ShellTests.test_printer_park_respects_minimum_card_width_and_offline_readability -v
  EXPECT: Ran 1 test
  EVIDENCE: Embedded browser at 1352×885 after stylesheet reload shows unknown progress as a dash; the empty progress ring/caption and empty temperature/fan scales are absent, telemetry labels remain readable, and printer commands remain unavailable offline.

- [x] G69: Queue workspace aligns with the 1362×892 reference frame and remains legible on mobile
  CHECK: python -m unittest connector.tests.test_v19_shell.PrintFlowV19ShellTests.test_queue_workspace_frame_matches_1362px_reference -v
  EXPECT: Ran 1 test
  EVIDENCE: Embedded browser at 1362×892: content left x≈254 vs reference x≈255, queue heading y≈94, KPI y≈188, toolbar y≈267, panels y≈336 vs reference y≈338. At 390×844 actions wrap without overlap, KPI form 2×2, controls remain visible with vertical scrolling, and mobile navigation is present. Live queue data and pixel-diff remain pending.

- [x] G70: Finance tabs fit the 390px mobile frame
  CHECK: python -m unittest connector.tests.test_v19_shell.PrintFlowV19ShellTests.test_finance_mobile_tabs_fit_narrow_viewport -v
  EXPECT: Ran 1 test
  EVIDENCE: Embedded browser at 390×844 shows “Деньги сейчас”, “Прибыль и P&L”, and “Налоги и отчёты” fully readable in one tab row. Cash pane remains the default; P&L and tax panes were separately opened and showed their own offline states. No financial actions were triggered.

- [x] G71: Embedded AI rail matches desktop width and active-chat surface
  CHECK: python -m unittest connector.tests.test_v19_shell.PrintFlowV19ShellTests.test_ai_rail_matches_specified_desktop_width_at_reference_viewport -v
  EXPECT: Ran 1 test
  EVIDENCE: Embedded browser at 1352×885 reports rail x=944, width=398px (reference x≈947, Δ3px); assistant iframe and shell both load nozza-design-v2.css v20.1.83. Active chat has a solid dark work surface with no workshop illustration, visible offline status/suggestions/composer, and blurred dashboard behind the rail. No message was sent.

- [x] G72: Orders page vertical frame matches its 1352×885 reference
  CHECK: python -m unittest connector.tests.test_v19_shell.PrintFlowV19ShellTests.test_orders_page_keeps_documented_scale_and_kanban_geometry -v
  EXPECT: Ran 1 test
  EVIDENCE: Embedded browser at 1352×885: content x=252; heading frame y=92; quick views y=188/h62; toolbar y=263/h58; kanban y=333 vs reference ≈334. Offline counts remain unknown and the list shows an explicit retry state, not sample order cards.

- [x] G73: Customers page matches its desktop frame and keeps aftercare counts unknown offline
  CHECK: python -m unittest connector.tests.test_v19_shell.PrintFlowV19ShellTests.test_customer_aftercare_offline_state_is_explicit_and_accessible -v
  EXPECT: Ran 1 test
  EVIDENCE: Embedded browser at 1352×885: header x252/y92; KPI badge y145; advice y187/h72; insight cards y273/h98; aftercare y385/h185; customer list title y≈600; table y655 vs reference y653. Aftercare shows three labeled unknown counters (—), and explicit unavailable status; customer data remain unknown, no outreach/action was sent.

- [x] G74: Printer park frame aligns with the 1352×885 reference at desktop width
  CHECK: python -m unittest connector.tests.test_v19_shell.PrintFlowV19ShellTests.test_printer_park_respects_minimum_card_width_and_offline_readability -v
  EXPECT: park top y≈187, content x=252 to right≈1316; mobile and tablet styles remain unchanged.
  EVIDENCE: Embedded browser at 1352×885 reports offline park x=252, y≈187, width≈1064; active printer commands remain unavailable.


- [x] G75: Warehouses header, four KPI cards, and offline workspace match the desktop reference frame
  CHECK: python -m unittest connector.tests.test_v19_shell.PrintFlowV19ShellTests.test_warehouse_empty_state_spans_grid_and_period_controls_are_readable -v
  EXPECT: tabs y≈187, KPI y≈259/h≈118, offline workspace y≈391/h≈376; ledger y≈783; desktop content x=252 to right≈1316.
  EVIDENCE: Embedded browser at 1352×885 confirms tabs y=187 and corrected x/right boundaries. Offline error retains the two-row warehouse-card footprint, so lower ledger stays at its reference position; backend totals remain unknown.




- [x] G76: Settings controls and header frame align without an offline banner displacing the panel
  CHECK: python -m unittest connector.tests.test_v19_shell.PrintFlowV19ShellTests.test_settings_header_actions_match_reference_alignment connector.tests.test_v19_shell.PrintFlowV19ShellTests.test_settings_offline_disables_legacy_form_controls -v
  EXPECT: categories y≈197; cards y≈349; search/reset/save aligned to the reference. All setting fields disabled while connection version is unknown.
  EVIDENCE: Embedded browser at 1352×885 reports categories y=197, cards y=351, search x=872, reset x=1197, save x=872; zero editable setting fields. Sidebar keeps connector-unavailable status visible.


- [x] G77: Print catalog exits loading cleanly when the API is unavailable and never reports zero forms
  CHECK: python -m unittest connector.tests.test_v19_shell.PrintFlowV19ShellTests.test_print_catalog_failure_shows_unknown_instead_of_spinner_and_zero -v
  EXPECT: Offline panel has a clear status, unknown counts (—), disabled filters/search, and preserves the 61-layout preview tab.
  EVIDENCE: Embedded browser at 1352×885 shows inline unavailable state (190px), tab/count labels as em dashes, filters disabled, and the separate 61-layout preview tab remains available. Static /api/print/forms returns 404 in this preview; no paper was printed.


- [x] G78: Shift center retains exact funnel, equipment, and production boundaries offline
  CHECK: python -m unittest connector.tests.test_v19_shell.PrintFlowV19ShellTests.test_shift_center_offline_cards_preserve_reference_frame -v
  EXPECT: funnel x252/y187/w1064/h200; equipment y405/h255; production y660. Unknown values remain dashes.
  EVIDENCE: Embedded browser at 1352×885 reports funnel y186.94/h200, equipment y404.94/h254.72, production y659.66; API-offline messages are visible and simulation stays disabled.


- [x] G79: Niches summary and offline hypothesis area align with the desktop reference
  CHECK: python -m unittest connector.tests.test_v19_shell.PrintFlowV19ShellTests.test_niches_offline_state_preserves_reference_card_height -v
  EXPECT: brief y≈187; summary y≈273; offline grid y≈391/h≈438; unknown counts stay dashes.
  EVIDENCE: Embedded browser at 1352×885 measured brief y186.94/h72, summary y272.94/h104.98, grid y391.92/h438; offline warning replaces data without fake niche cards.

- [x] G80: Dashboard live-shop and briefing frame match the 1440×900 reference while offline
  CHECK: python -m unittest connector.tests.test_v19_shell.PrintFlowV19ShellTests.test_dashboard_offline_live_shop_preserves_reference_frame -v
  EXPECT: content x255…1391; live row y≈189/h568; columns ≈773/351; offline status centered; KPI row y≈769.
  EVIDENCE: Embedded browser at 1440×900 measured content x255/w1136, row x255/y188.94/w1136/h568, hero w772.75, briefing x1039.75/w351.25, KPIs y768.94; unavailable values remain dashes.

- [x] G82: Batches navigation and KPI geometry align with the desktop reference
  CHECK: python -m unittest connector.tests.test_v19_shell.PrintFlowV19ShellTests.test_batches_desktop_reference_spacing_and_kpi_height -v
  EXPECT: navigation y≈189/h52; KPI y≈261/h118; batch list y≈394; offline values stay unknown.
  EVIDENCE: Embedded browser at 1352×885 measured navigation x252/y188.94/w1064/h52, KPI y260.94/h119, list y393.94; no sample batch or production count is shown.

- [x] G83: Calculator work area begins at the reference y-coordinate on desktop
  CHECK: python -m unittest connector.tests.test_v19_shell.PrintFlowV19ShellTests.test_calculator_desktop_header_gap_matches_reference -v
  EXPECT: at 1362×892, grid begins at y≈293; input/result widths retain 60/40 proportions; offline result stays unknown.
  EVIDENCE: Embedded browser measured grid x252/y292.94/w1064, inputs w630 and sticky results w420; no calculated price was fabricated.

- [x] G84: Shift center offline production status preserves the reference row height
  CHECK: python -m unittest connector.tests.test_v19_shell.PrintFlowV19ShellTests.test_shift_center_production_offline_status_keeps_one_row_footprint -v
  EXPECT: production starts y≈660 and ends y≈850; unavailable status is compact, unknown values stay dashes.
  EVIDENCE: Embedded browser at 1352×885 measured production x252/y659.66/h≈192 and unavailable row x269/y805.84; final 28 px verification follows after cache refresh.

