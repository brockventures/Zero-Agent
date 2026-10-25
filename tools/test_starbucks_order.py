"""Unit tests for Starbucks Android Native App Automation (tools/starbucks_order.py)."""

from __future__ import annotations

import json
import os
import sys
import unittest
from unittest.mock import MagicMock, call, patch
import xml.etree.ElementTree as ET

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
if "/workspace" not in sys.path:
    sys.path.insert(0, "/workspace")

from tools.starbucks_order import (
    DEFAULT_DUMP_FILE,
    STARBUCKS_PKG,
    UINode,
    check_and_handle_relogin,
    dismiss_modals,
    dump_ui_tree,
    enter_text,
    execute_checkout,
    extract_visible_nodes,
    find_all_nodes,
    find_node,
    get_starbucks_credentials,
    get_status,
    list_favorites,
    list_stores,
    parse_bounds,
    press_key,
    search_menu,
    stage_order,
    starbucks_session,
    tap_coords,
    tap_node,
)

SAMPLE_HOME_XML = """<?xml version="1.0" encoding="utf-8"?>
<hierarchy rotation="0">
  <node index="0" text="" resource-id="com.starbucks.mobilecard:id/action_bar" class="android.view.ViewGroup" package="com.starbucks.mobilecard" content-desc="" checkable="false" checked="false" clickable="false" enabled="true" focusable="false" focused="false" scrollable="false" long-clickable="false" password="false" selected="false" bounds="[0,0][1080,200]">
    <node index="0" text="Good afternoon, Ryan" resource-id="com.starbucks.mobilecard:id/greeting" class="android.widget.TextView" package="com.starbucks.mobilecard" content-desc="Good afternoon, Ryan" checkable="false" checked="false" clickable="false" enabled="true" focusable="false" focused="false" scrollable="false" long-clickable="false" password="false" selected="false" bounds="[48,60][600,140]" />
  </node>
  <node index="1" text="" resource-id="com.starbucks.mobilecard:id/rewards_card" class="android.widget.FrameLayout" package="com.starbucks.mobilecard" content-desc="" checkable="false" checked="false" clickable="true" enabled="true" focusable="true" focused="false" scrollable="false" long-clickable="false" password="false" selected="false" bounds="[48,220][1032,600]">
    <node index="0" text="175 ★" resource-id="com.starbucks.mobilecard:id/star_balance" class="android.widget.TextView" package="com.starbucks.mobilecard" content-desc="175 Stars" checkable="false" checked="false" clickable="false" enabled="true" focusable="false" focused="false" scrollable="false" long-clickable="false" password="false" selected="false" bounds="[72,250][300,320]" />
    <node index="1" text="Card Balance $18.50" resource-id="com.starbucks.mobilecard:id/card_balance" class="android.widget.TextView" package="com.starbucks.mobilecard" content-desc="Card balance eighteen dollars fifty cents" checkable="false" checked="false" clickable="false" enabled="true" focusable="false" focused="false" scrollable="false" long-clickable="false" password="false" selected="false" bounds="[72,330][450,380]" />
    <node index="2" text="Pickup at Stevens Creek &amp; Winchester" resource-id="com.starbucks.mobilecard:id/store_name" class="android.widget.TextView" package="com.starbucks.mobilecard" content-desc="Pickup at Stevens Creek &amp; Winchester" checkable="false" checked="false" clickable="true" enabled="true" focusable="false" focused="false" scrollable="false" long-clickable="false" password="false" selected="false" bounds="[72,400][800,460]" />
    <node index="3" text="In store" resource-id="com.starbucks.mobilecard:id/pickup_type" class="android.widget.TextView" package="com.starbucks.mobilecard" content-desc="In store pickup" checkable="false" checked="false" clickable="false" enabled="true" focusable="false" focused="false" scrollable="false" long-clickable="false" password="false" selected="false" bounds="[72,470][250,520]" />
  </node>
  <node index="2" text="" resource-id="com.starbucks.mobilecard:id/bottom_navigation" class="android.widget.LinearLayout" package="com.starbucks.mobilecard" content-desc="" checkable="false" checked="false" clickable="false" enabled="true" focusable="false" focused="false" scrollable="false" long-clickable="false" password="false" selected="false" bounds="[0,2100][1080,2300]">
    <node index="0" text="Home" resource-id="com.starbucks.mobilecard:id/nav_home" class="android.widget.TextView" package="com.starbucks.mobilecard" content-desc="Home" checkable="false" checked="false" clickable="true" enabled="true" focusable="true" focused="false" scrollable="false" long-clickable="false" password="false" selected="true" bounds="[0,2100][270,2300]" />
    <node index="1" text="Order" resource-id="com.starbucks.mobilecard:id/nav_order" class="android.widget.TextView" package="com.starbucks.mobilecard" content-desc="Order" checkable="false" checked="false" clickable="true" enabled="true" focusable="true" focused="false" scrollable="false" long-clickable="false" password="false" selected="false" bounds="[270,2100][540,2300]" />
  </node>
</hierarchy>
"""

SAMPLE_SIGN_IN_XML = """<?xml version="1.0" encoding="utf-8"?>
<hierarchy rotation="0">
  <node index="0" text="Welcome to Starbucks" resource-id="com.starbucks.mobilecard:id/welcome_title" class="android.widget.TextView" package="com.starbucks.mobilecard" content-desc="Welcome" bounds="[100,300][980,450]" />
  <node index="1" text="Sign in" resource-id="com.starbucks.mobilecard:id/btn_sign_in" class="android.widget.Button" package="com.starbucks.mobilecard" content-desc="Sign in" clickable="true" bounds="[100,500][980,620]" />
  <node index="2" text="Join now" resource-id="com.starbucks.mobilecard:id/btn_join" class="android.widget.Button" package="com.starbucks.mobilecard" content-desc="Join now" clickable="true" bounds="[100,650][980,770]" />
</hierarchy>
"""

SAMPLE_MODAL_XML = """<?xml version="1.0" encoding="utf-8"?>
<hierarchy rotation="0">
  <node index="0" text="What's New in Starbucks" resource-id="com.starbucks.mobilecard:id/dialog_title" class="android.widget.TextView" package="com.starbucks.mobilecard" content-desc="" bounds="[100,400][980,550]" />
  <node index="1" text="Got it" resource-id="com.starbucks.mobilecard:id/btn_dismiss" class="android.widget.Button" package="com.starbucks.mobilecard" content-desc="Got it" clickable="true" bounds="[400,1200][680,1300]" />
</hierarchy>
"""

SAMPLE_STORES_XML = """<?xml version="1.0" encoding="utf-8"?>
<hierarchy rotation="0">
  <node index="0" text="Nearby Stores" resource-id="com.starbucks.mobilecard:id/title" class="android.widget.TextView" package="com.starbucks.mobilecard" bounds="[48,80][500,160]" />
  <node index="1" text="El Camino &amp; Scott" resource-id="com.starbucks.mobilecard:id/store_name" class="android.widget.TextView" package="com.starbucks.mobilecard" clickable="true" bounds="[48,200][800,260]" />
  <node index="2" text="0.4 mi" resource-id="com.starbucks.mobilecard:id/store_dist" class="android.widget.TextView" package="com.starbucks.mobilecard" bounds="[820,200][1000,260]" />
  <node index="3" text="1234 El Camino Real, Santa Clara, CA" resource-id="com.starbucks.mobilecard:id/store_addr" class="android.widget.TextView" package="com.starbucks.mobilecard" bounds="[48,270][900,320]" />
  <node index="4" text="Stevens Creek &amp; Winchester" resource-id="com.starbucks.mobilecard:id/store_name" class="android.widget.TextView" package="com.starbucks.mobilecard" clickable="true" bounds="[48,400][800,460]" />
  <node index="5" text="1.1 mi" resource-id="com.starbucks.mobilecard:id/store_dist" class="android.widget.TextView" package="com.starbucks.mobilecard" bounds="[820,400][1000,460]" />
  <node index="6" text="2855 Stevens Creek Blvd, Santa Clara, CA" resource-id="com.starbucks.mobilecard:id/store_addr" class="android.widget.TextView" package="com.starbucks.mobilecard" bounds="[48,470][900,520]" />
</hierarchy>
"""

SAMPLE_FAVORITES_XML = """<?xml version="1.0" encoding="utf-8"?>
<hierarchy rotation="0">
  <node index="0" text="Favorites" resource-id="com.starbucks.mobilecard:id/tab_favorites" class="android.widget.TextView" package="com.starbucks.mobilecard" selected="true" bounds="[48,150][300,220]" />
  <node index="1" text="Iced Brown Sugar Oatmilk Shaken Espresso" resource-id="com.starbucks.mobilecard:id/item_name" class="android.widget.TextView" package="com.starbucks.mobilecard" clickable="true" bounds="[48,260][900,320]" />
  <node index="2" text="Grande, 120 cal" resource-id="com.starbucks.mobilecard:id/item_details" class="android.widget.TextView" package="com.starbucks.mobilecard" bounds="[48,330][400,380]" />
  <node index="3" text="$5.95" resource-id="com.starbucks.mobilecard:id/item_price" class="android.widget.TextView" package="com.starbucks.mobilecard" bounds="[48,390][200,440]" />
  <node index="4" text="Caffe Latte" resource-id="com.starbucks.mobilecard:id/item_name" class="android.widget.TextView" package="com.starbucks.mobilecard" clickable="true" bounds="[48,500][900,560]" />
  <node index="5" text="Venti, 250 cal" resource-id="com.starbucks.mobilecard:id/item_details" class="android.widget.TextView" package="com.starbucks.mobilecard" bounds="[48,570][400,620]" />
  <node index="6" text="$5.25" resource-id="com.starbucks.mobilecard:id/item_price" class="android.widget.TextView" package="com.starbucks.mobilecard" bounds="[48,630][200,680]" />
</hierarchy>
"""

SAMPLE_SEARCH_XML = """<?xml version="1.0" encoding="utf-8"?>
<hierarchy rotation="0">
  <node index="0" text="Search results" resource-id="com.starbucks.mobilecard:id/search_title" class="android.widget.TextView" package="com.starbucks.mobilecard" bounds="[48,150][400,220]" />
  <node index="1" text="Cold Brew Coffee" resource-id="com.starbucks.mobilecard:id/item_name" class="android.widget.TextView" package="com.starbucks.mobilecard" clickable="true" bounds="[48,260][800,320]" />
  <node index="2" text="$4.45" resource-id="com.starbucks.mobilecard:id/item_price" class="android.widget.TextView" package="com.starbucks.mobilecard" bounds="[48,330][200,380]" />
  <node index="3" text="Vanilla Sweet Cream Cold Brew" resource-id="com.starbucks.mobilecard:id/item_name" class="android.widget.TextView" package="com.starbucks.mobilecard" clickable="true" bounds="[48,450][800,510]" />
  <node index="4" text="$5.25" resource-id="com.starbucks.mobilecard:id/item_price" class="android.widget.TextView" package="com.starbucks.mobilecard" bounds="[48,520][200,570]" />
</hierarchy>
"""

SAMPLE_CHECKOUT_REVIEW_XML = """<?xml version="1.0" encoding="utf-8"?>
<hierarchy rotation="0">
  <node index="0" text="Review Order" resource-id="com.starbucks.mobilecard:id/title" class="android.widget.TextView" package="com.starbucks.mobilecard" bounds="[48,80][500,160]" />
  <node index="1" text="Pickup at Stevens Creek &amp; Winchester" resource-id="com.starbucks.mobilecard:id/store_header" class="android.widget.TextView" package="com.starbucks.mobilecard" bounds="[48,200][900,260]" />
  <node index="2" text="Estimated pickup: 5-8 mins" resource-id="com.starbucks.mobilecard:id/pickup_time" class="android.widget.TextView" package="com.starbucks.mobilecard" bounds="[48,270][700,330]" />
  <node index="3" text="Iced Brown Sugar Oatmilk Shaken Espresso" resource-id="com.starbucks.mobilecard:id/cart_item" class="android.widget.TextView" package="com.starbucks.mobilecard" bounds="[48,400][900,460]" />
  <node index="4" text="Subtotal" resource-id="com.starbucks.mobilecard:id/lbl_subtotal" class="android.widget.TextView" package="com.starbucks.mobilecard" bounds="[48,800][300,850]" />
  <node index="5" text="$5.95" resource-id="com.starbucks.mobilecard:id/val_subtotal" class="android.widget.TextView" package="com.starbucks.mobilecard" bounds="[800,800][1000,850]" />
  <node index="6" text="Estimated tax" resource-id="com.starbucks.mobilecard:id/lbl_tax" class="android.widget.TextView" package="com.starbucks.mobilecard" bounds="[48,860][300,910]" />
  <node index="7" text="$0.55" resource-id="com.starbucks.mobilecard:id/val_tax" class="android.widget.TextView" package="com.starbucks.mobilecard" bounds="[800,860][1000,910]" />
  <node index="8" text="Total" resource-id="com.starbucks.mobilecard:id/lbl_total" class="android.widget.TextView" package="com.starbucks.mobilecard" bounds="[48,920][300,980]" />
  <node index="9" text="$6.50" resource-id="com.starbucks.mobilecard:id/val_total" class="android.widget.TextView" package="com.starbucks.mobilecard" bounds="[800,920][1000,980]" />
  <node index="10" text="Starbucks Card (...0042)" resource-id="com.starbucks.mobilecard:id/payment_method" class="android.widget.TextView" package="com.starbucks.mobilecard" bounds="[48,1050][600,1110]" />
  <node index="11" text="Place Order" resource-id="com.starbucks.mobilecard:id/btn_checkout" class="android.widget.Button" package="com.starbucks.mobilecard" clickable="true" bounds="[100,1800][980,1950]" />
</hierarchy>
"""

SAMPLE_CONFIRMATION_XML = """<?xml version="1.0" encoding="utf-8"?>
<hierarchy rotation="0">
  <node index="0" text="Thank you! Order Confirmed #104" resource-id="com.starbucks.mobilecard:id/confirm_title" class="android.widget.TextView" package="com.starbucks.mobilecard" bounds="[48,200][1000,320]" />
  <node index="1" text="Ready in 5-8 mins" resource-id="com.starbucks.mobilecard:id/pickup_time" class="android.widget.TextView" package="com.starbucks.mobilecard" bounds="[48,350][700,410]" />
  <node index="2" text="Pickup at Stevens Creek &amp; Winchester" resource-id="com.starbucks.mobilecard:id/store_name" class="android.widget.TextView" package="com.starbucks.mobilecard" bounds="[48,430][900,490]" />
</hierarchy>
"""


class TestStarbucksOrder(unittest.TestCase):

    def test_parse_bounds_valid(self):
        rect, center = parse_bounds("[100,200][300,400]")
        self.assertEqual(rect, (100, 200, 300, 400))
        self.assertEqual(center, (200, 300))

    def test_parse_bounds_invalid(self):
        rect, center = parse_bounds("invalid_bounds")
        self.assertEqual(rect, (0, 0, 0, 0))
        self.assertEqual(center, (0, 0))

        rect_empty, center_empty = parse_bounds("")
        self.assertEqual(rect_empty, (0, 0, 0, 0))
        self.assertEqual(center_empty, (0, 0))

    def test_extract_visible_nodes_and_find_node(self):
        nodes = extract_visible_nodes(SAMPLE_HOME_XML)
        self.assertGreater(len(nodes), 4)

        greeting_node = find_node(nodes, resource_id="greeting")
        self.assertIsNotNone(greeting_node)
        self.assertEqual(greeting_node.text, "Good afternoon, Ryan")
        self.assertTrue(greeting_node.is_visible)
        self.assertEqual(greeting_node.center, ((48 + 600) // 2, (60 + 140) // 2))

        # Test text search
        order_tab = find_node(nodes, text="Order")
        self.assertIsNotNone(order_tab)
        self.assertTrue(order_tab.clickable)

        # Test find_all_nodes
        all_textviews = find_all_nodes(nodes, class_name="TextView")
        self.assertGreaterEqual(len(all_textviews), 5)

    def test_extract_visible_nodes_empty_or_malformed(self):
        self.assertEqual(extract_visible_nodes(""), [])
        self.assertEqual(extract_visible_nodes("not xml content"), [])

    @patch("tools.starbucks_order.run_adb_shell")
    def test_tap_coords_and_tap_node(self, mock_adb_shell):
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_adb_shell.return_value = mock_proc

        self.assertTrue(tap_coords(100, 200))
        mock_adb_shell.assert_called_with(["input", "tap", "100", "200"])

        node = UINode(center=(250, 450))
        success = tap_node(node)
        self.assertTrue(success)
        mock_adb_shell.assert_called_with(["input", "tap", "250", "450"])

        # Invalid center
        invalid_node = UINode(center=(0, 0))
        self.assertFalse(tap_node(invalid_node))

    @patch("tools.starbucks_order.run_adb_shell")
    def test_enter_text_and_press_key(self, mock_adb_shell):
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_adb_shell.return_value = mock_proc

        # Without clear_first
        success = enter_text("cold brew")
        self.assertTrue(success)
        # Spaces converted to %s
        mock_adb_shell.assert_called_with(["input", "text", "cold%sbrew"])

        # With clear_first
        mock_adb_shell.reset_mock()
        enter_text("latte", clear_first=True)
        # Verifies keyevents 123 (MOVE_END) and 67 (DEL) were fired
        calls = [c[0][0] for c in mock_adb_shell.call_args_list]
        self.assertTrue(any("123" in arg_list for arg_list in calls))
        self.assertTrue(any("67" in arg_list for arg_list in calls))

        # Test press_key
        mock_adb_shell.reset_mock()
        press_key(66)
        mock_adb_shell.assert_called_with(["input", "keyevent", "66"])

    @patch("tools.starbucks_order.tap_coords")
    def test_dismiss_modals(self, mock_tap_coords):
        mock_tap_coords.return_value = True
        dismissed = dismiss_modals(root=SAMPLE_MODAL_XML, max_passes=1)
        self.assertEqual(dismissed, 1)
        # Center of [400,1200][680,1300] is (540, 1250)
        mock_tap_coords.assert_called_with(540, 1250)

    @patch("tools.starbucks_order.get_starbucks_credentials")
    @patch("tools.starbucks_order.dump_ui_tree")
    @patch("tools.starbucks_order.enter_text")
    @patch("tools.starbucks_order.tap_node")
    def test_relogin_flow(self, mock_tap, mock_enter_text, mock_dump, mock_creds):
        mock_creds.return_value = ("test@example.com", "SecretPass123!")

        login_form_xml = """<?xml version="1.0" encoding="utf-8"?>
        <hierarchy rotation="0">
          <node index="0" text="" resource-id="com.starbucks.mobilecard:id/et_email" class="android.widget.EditText" bounds="[100,300][980,400]" />
          <node index="1" text="" resource-id="com.starbucks.mobilecard:id/et_password" class="android.widget.EditText" password="true" bounds="[100,450][980,550]" />
          <node index="2" text="Sign in" resource-id="com.starbucks.mobilecard:id/btn_submit" class="android.widget.Button" clickable="true" bounds="[100,600][980,720]" />
        </hierarchy>
        """

        mock_dump.side_effect = [
            ET.fromstring(login_form_xml),
            ET.fromstring(login_form_xml),
            ET.fromstring(SAMPLE_HOME_XML),
        ]

        handled = check_and_handle_relogin(root=SAMPLE_SIGN_IN_XML)
        self.assertTrue(handled)
        mock_enter_text.assert_has_calls([
            call("test@example.com", clear_first=True),
            call("SecretPass123!", clear_first=True),
        ])

    def test_get_starbucks_credentials_env_and_file(self):
        # 1. From environment variables
        with patch.dict(os.environ, {"STARBUCKS_EMAIL": "env@example.com", "STARBUCKS_PASSWORD": "EnvPassword1!"}):
            em, pw = get_starbucks_credentials()
            self.assertEqual(em, "env@example.com")
            self.assertEqual(pw, "EnvPassword1!")

        # 2. From /secrets/env.json fallback
        with patch.dict(os.environ, {}, clear=True):
            with patch("pathlib.Path.exists", return_value=True):
                mock_json_content = json.dumps({
                    "STARBUCKS_EMAIL": "secret@example.com",
                    "STARBUCKS_PASSWORD": "SecretPassword2@",
                })
                with patch("pathlib.Path.read_text", return_value=mock_json_content):
                    em, pw = get_starbucks_credentials()
                    self.assertEqual(em, "secret@example.com")
                    self.assertEqual(pw, "SecretPassword2@")

    @patch("tools.starbucks_order.check_printer_conflict")
    def test_starbucks_session_printer_conflict(self, mock_conflict):
        mock_conflict.return_value = (True, "IP Webcam active on port 3333")

        with self.assertRaises(RuntimeError) as ctx:
            with starbucks_session():
                pass
        self.assertIn("Printer conflict guard active", str(ctx.exception))

    @patch("tools.starbucks_order.sleep_screen")
    @patch("tools.starbucks_order.launch_starbucks_app")
    @patch("tools.starbucks_order.dismiss_modals")
    @patch("tools.starbucks_order.check_and_handle_relogin")
    @patch("tools.starbucks_order.wake_and_unlock")
    @patch("tools.starbucks_order.check_printer_conflict")
    def test_starbucks_session_force_and_sleep_guarantee(
        self, mock_conflict, mock_wake, mock_relogin, mock_dismiss, mock_launch, mock_sleep
    ):
        mock_conflict.return_value = (True, "IP Webcam active")

        # Session with force=True proceeds and unconditionally calls sleep_screen
        with starbucks_session(force=True):
            mock_wake.assert_called_once()
            mock_launch.assert_called_once()
            mock_dismiss.assert_called_once()

        mock_sleep.assert_called_once()

        # Exception safety: sleep_screen still called on exception
        mock_sleep.reset_mock()
        try:
            with starbucks_session(force=True):
                raise ValueError("Unexpected error during session")
        except ValueError:
            pass
        mock_sleep.assert_called_once()

    def test_get_status_authenticated_parsing(self):
        status = get_status(root=SAMPLE_HOME_XML)
        self.assertEqual(status["package"], STARBUCKS_PKG)
        self.assertTrue(status["authenticated"])
        self.assertEqual(status["auth_state"], "authenticated")
        self.assertEqual(status["stars"], 175)
        self.assertEqual(status["balance"], "$18.50")
        self.assertEqual(status["active_store"], "Stevens Creek & Winchester")
        self.assertEqual(status["pickup_type"], "In store")

    def test_get_status_unauthenticated_parsing(self):
        status = get_status(root=SAMPLE_SIGN_IN_XML)
        self.assertFalse(status["authenticated"])
        self.assertEqual(status["auth_state"], "unauthenticated")
        self.assertIsNone(status["stars"])
        self.assertIsNone(status["balance"])

    def test_list_stores_and_select(self):
        res = list_stores(root=SAMPLE_STORES_XML)
        self.assertEqual(res["count"], 2)
        stores = res["stores"]
        self.assertEqual(stores[0]["name"], "El Camino & Scott")
        self.assertEqual(stores[0]["distance"], "0.4 mi")
        self.assertEqual(stores[1]["name"], "Stevens Creek & Winchester")
        self.assertEqual(stores[1]["distance"], "1.1 mi")

        # Test selecting a store
        res_sel = list_stores(select="winchester", root=SAMPLE_STORES_XML)
        self.assertEqual(res_sel["selected"], "Stevens Creek & Winchester")

    def test_list_favorites(self):
        res = list_favorites(root=SAMPLE_FAVORITES_XML)
        self.assertEqual(res["count"], 2)
        favs = res["favorites"]
        self.assertEqual(favs[0]["name"], "Iced Brown Sugar Oatmilk Shaken Espresso")
        self.assertEqual(favs[0]["details"], "Grande, 120 cal")
        self.assertEqual(favs[0]["price"], "$5.95")
        self.assertEqual(favs[1]["name"], "Caffe Latte")
        self.assertEqual(favs[1]["price"], "$5.25")

    def test_search_menu(self):
        res = search_menu(query="cold brew", root=SAMPLE_SEARCH_XML)
        self.assertEqual(res["query"], "cold brew")
        self.assertEqual(res["count"], 2)
        self.assertEqual(res["results"][0]["name"], "Cold Brew Coffee")
        self.assertEqual(res["results"][0]["price"], "$4.45")
        self.assertEqual(res["results"][1]["name"], "Vanilla Sweet Cream Cold Brew")
        self.assertEqual(res["results"][1]["price"], "$5.25")

    def test_stage_order_stops_before_payment(self):
        # Cart staging must parse the review screen and return price/summary
        # WITHOUT paying
        res = stage_order(
            item="Iced Brown Sugar Oatmilk Shaken Espresso",
            root=SAMPLE_CHECKOUT_REVIEW_XML,
        )
        self.assertEqual(res["status"], "staged")
        self.assertEqual(res["item_requested"], "Iced Brown Sugar Oatmilk Shaken Espresso")
        self.assertEqual(res["store"], "Stevens Creek & Winchester")
        self.assertEqual(res["pickup_estimate"], "5-8 mins")
        self.assertEqual(res["subtotal"], "$5.95")
        self.assertEqual(res["tax"], "$0.55")
        self.assertEqual(res["total_price"], "$6.50")
        self.assertEqual(res["payment_method"], "Starbucks Card")
        self.assertTrue(res["ready_for_checkout"])
        self.assertIn("Pre-confirmed only", res["safety_gate"])

    def test_checkout_safety_gate_rejected_without_confirm(self):
        res = execute_checkout(confirm_checkout=False)
        self.assertEqual(res["status"], "error")
        self.assertFalse(res["order_placed"])
        self.assertIn("--confirm-checkout must be explicitly passed", res["error"])

    def test_checkout_confirmed_places_order(self):
        res = execute_checkout(confirm_checkout=True, root=SAMPLE_CONFIRMATION_XML)
        self.assertEqual(res["status"], "confirmed")
        self.assertTrue(res["order_placed"])
        self.assertIn("Thank you! Order Confirmed #104", res["confirmation_message"])
        self.assertEqual(res["pickup_estimate"], "5-8 mins")
        self.assertEqual(res["store"], "Stevens Creek & Winchester")

    @patch("tools.starbucks_order.run_adb_shell")
    def test_dump_ui_tree(self, mock_adb_shell):
        mock_proc_dump = MagicMock(returncode=0, stdout="UI hierchary dumped to: /data/local/tmp/uidump.xml")
        mock_proc_cat = MagicMock(returncode=0, stdout=SAMPLE_HOME_XML)
        mock_adb_shell.side_effect = [mock_proc_dump, mock_proc_cat]

        tree = dump_ui_tree()
        self.assertIsNotNone(tree)
        self.assertEqual(tree.tag, "hierarchy")
        mock_adb_shell.assert_has_calls([
            call(["uiautomator", "dump", DEFAULT_DUMP_FILE]),
            call(["cat", DEFAULT_DUMP_FILE]),
        ])


if __name__ == "__main__":
    unittest.main()
