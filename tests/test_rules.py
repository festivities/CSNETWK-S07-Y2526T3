"""
Unit tests for the rules engine, with no sockets involved.

These drive the rules modules directly against a hand-built GameState, which makes
the combat arithmetic, mana payment and card effects fast and completely
deterministic -- no shuffling, no network, no timing.

A real GameEngine is used with an empty `connections` map: GameEngine.send simply
returns when a player has no connection, so broadcasts become no-ops and the rules
can be exercised on their own.

    python -m unittest discover -s tests -v
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mtgnp import cards, combat, effects, mana, priority, protocol   # noqa: E402
from mtgnp.engine import GameEngine                                   # noqa: E402
from mtgnp.state import GameOver, GameState, Permanent, StackItem     # noqa: E402
from mtgnp.verbose import VerboseLogger                               # noqa: E402

ONE, TWO = "player_1", "player_2"


def make_engine():
    """A ready-to-use engine on turn 1 with player_1 active and no connections."""
    state = GameState([ONE, TWO])
    state.player_order = [ONE, TWO]
    state.active_player = ONE
    state.turn = 1
    state.phase = protocol.PRECOMBAT_MAIN

    engine = GameEngine(VerboseLogger("TEST", enabled=False))
    engine.state = state
    engine.connections = {}
    return engine, state


def put(state, player_id, card_id, tapped=False, summoning_sick=False):
    """Place a permanent on the battlefield and return it."""
    permanent = Permanent(card_id=card_id, controller=player_id,
                          tapped=tapped, summoning_sick=summoning_sick)
    state.player(player_id).battlefield.append(permanent)
    return permanent


class ScriptedEngine(GameEngine):
    """An engine whose `await_action` replays canned client PDUs.

    This exercises the parts of combat that block waiting for a declaration --
    the tapping of attackers, and the ASSIGN_DAMAGE_ORDER retry loop -- without
    needing sockets or a second player.
    """

    def __init__(self, state, responses):
        super().__init__(VerboseLogger("TEST", enabled=False))
        self.state = state
        self.connections = {}
        self.responses = list(responses)
        self.errors = []

    def await_action(self, player_id, allowed_types, expected_seq, regrant=None):
        return self.responses.pop(0)

    def send_error(self, player_id, code, message, rejected=None):
        self.errors.append((code, message))


# --- Combat damage (RFC Sections 9.6, 9.7) -----------------------------

class FirstStrikeTests(unittest.TestCase):

    def test_first_strike_kills_the_blocker_before_it_strikes_back(self):
        """White Knight (2/2 first strike) beats Grizzly Bears (2/2) unharmed."""
        engine, state = make_engine()
        knight = put(state, ONE, "white_knight_001")
        bears = put(state, TWO, "grizzly_bears_001")
        state.attackers = {"white_knight_001": TWO}
        state.blocks = {"white_knight_001": ["grizzly_bears_001"]}

        self.assertTrue(combat._anyone_has_first_strike(engine))

        # First Strike Damage Step: only the knight deals damage.
        combat.deal_combat_damage(engine, first_strike_step=True)
        self.assertIsNone(state.find_permanent("grizzly_bears_001"))
        self.assertIn("grizzly_bears_001", state.player(TWO).graveyard)
        self.assertEqual(knight.damage, 0)

        # Regular Combat Damage Step: a first striker does not strike twice, and
        # the blocker is already dead, so nothing further happens.
        combat.deal_combat_damage(engine, first_strike_step=False)
        self.assertEqual(knight.damage, 0)
        self.assertIsNotNone(state.find_permanent("white_knight_001"))

    def test_without_first_strike_both_creatures_die(self):
        engine, state = make_engine()
        put(state, ONE, "grizzly_bears_001")
        put(state, TWO, "grizzly_bears_002")
        state.attackers = {"grizzly_bears_001": TWO}
        state.blocks = {"grizzly_bears_001": ["grizzly_bears_002"]}

        self.assertFalse(combat._anyone_has_first_strike(engine))
        combat.deal_combat_damage(engine, first_strike_step=False)

        self.assertIsNone(state.find_permanent("grizzly_bears_001"))
        self.assertIsNone(state.find_permanent("grizzly_bears_002"))

    def test_which_creatures_deal_damage_in_each_step(self):
        engine, state = make_engine()
        first_striker = put(state, ONE, "white_knight_001")
        ordinary = put(state, ONE, "grizzly_bears_001")

        self.assertTrue(combat._deals_damage_now(first_striker, first_strike_step=True))
        self.assertFalse(combat._deals_damage_now(first_striker, first_strike_step=False))
        self.assertFalse(combat._deals_damage_now(ordinary, first_strike_step=True))
        self.assertTrue(combat._deals_damage_now(ordinary, first_strike_step=False))

    def test_unblocked_attacker_hits_the_player(self):
        engine, state = make_engine()
        put(state, ONE, "grizzly_bears_001")
        state.attackers = {"grizzly_bears_001": TWO}
        state.blocks = {}

        combat.deal_combat_damage(engine, first_strike_step=False)
        self.assertEqual(state.player(TWO).life, 18)

    def test_blocked_attacker_never_hits_the_player(self):
        """MTGNP 1.0 has no trample (RFC Section 9.7)."""
        engine, state = make_engine()
        put(state, ONE, "leatherback_baloth_001")   # 4/5
        put(state, TWO, "ornithopter_001")          # 0/2
        state.attackers = {"leatherback_baloth_001": TWO}
        state.blocks = {"leatherback_baloth_001": ["ornithopter_001"]}

        combat.deal_combat_damage(engine, first_strike_step=False)
        self.assertEqual(state.player(TWO).life, 20)      # No damage got through.
        self.assertIsNone(state.find_permanent("ornithopter_001"))


class DamageOrderTests(unittest.TestCase):

    def setUp(self):
        self.engine, self.state = make_engine()
        put(self.state, ONE, "leatherback_baloth_001")   # 4/5 attacker
        put(self.state, TWO, "grizzly_bears_001")        # 2/2 blocker
        put(self.state, TWO, "savannah_lions_001")       # 2/1 blocker
        self.state.attackers = {"leatherback_baloth_001": TWO}
        self.state.blocks = {"leatherback_baloth_001":
                             ["grizzly_bears_001", "savannah_lions_001"]}
        self.attacker = self.state.find_permanent("leatherback_baloth_001")

    def assign(self, order):
        self.state.damage_order = {"leatherback_baloth_001": order}
        return combat._assign_to_blockers(
            self.state, self.attacker, "leatherback_baloth_001",
            self.state.blocks["leatherback_baloth_001"])

    def test_lethal_is_assigned_in_order_then_the_rest_overflows(self):
        """Bears first: 2 (lethal) to Bears, the remaining 2 to the Lions."""
        events = self.assign(["grizzly_bears_001", "savannah_lions_001"])
        self.assertEqual([(e["target"], e["amount"]) for e in events],
                         [("grizzly_bears_001", 2), ("savannah_lions_001", 2)])

    def test_a_different_order_changes_the_split(self):
        """Lions first: only 1 is lethal there, so Bears receives 3."""
        events = self.assign(["savannah_lions_001", "grizzly_bears_001"])
        self.assertEqual([(e["target"], e["amount"]) for e in events],
                         [("savannah_lions_001", 1), ("grizzly_bears_001", 3)])

    def test_damage_order_step_is_required_only_when_multiply_blocked(self):
        multiply_blocked = [a for a, b in self.state.blocks.items() if len(b) >= 2]
        self.assertEqual(multiply_blocked, ["leatherback_baloth_001"])

        self.state.blocks = {"leatherback_baloth_001": ["grizzly_bears_001"]}
        self.assertEqual([a for a, b in self.state.blocks.items() if len(b) >= 2], [])


class DeclarationLegalityTests(unittest.TestCase):

    def test_tapped_and_summoning_sick_and_defender_cannot_attack(self):
        engine, state = make_engine()
        put(state, ONE, "grizzly_bears_001", tapped=True)
        put(state, ONE, "grizzly_bears_002", summoning_sick=True)
        put(state, ONE, "wall_of_stone_001")
        put(state, ONE, "monastery_swiftspear_001", summoning_sick=True)

        def problem(creature_id):
            return combat._check_attackers(
                engine, ONE, TWO, [{"creature_id": creature_id, "target": TWO}])

        self.assertIn("tapped", problem("grizzly_bears_001"))
        self.assertIn("summoning sickness", problem("grizzly_bears_002"))
        self.assertIn("defender", problem("wall_of_stone_001"))
        # Haste beats summoning sickness.
        self.assertIsNone(problem("monastery_swiftspear_001"))

    def test_attacking_twice_with_one_creature_is_rejected(self):
        engine, state = make_engine()
        put(state, ONE, "grizzly_bears_001")
        problem = combat._check_attackers(engine, ONE, TWO, [
            {"creature_id": "grizzly_bears_001", "target": TWO},
            {"creature_id": "grizzly_bears_001", "target": TWO},
        ])
        self.assertIn("twice", problem)

    def test_flying_can_only_be_blocked_by_flying(self):
        engine, state = make_engine()
        put(state, ONE, "air_elemental_001")          # 4/4 flying
        put(state, TWO, "grizzly_bears_001")          # no flying
        put(state, TWO, "ornithopter_001")            # flying
        state.attackers = {"air_elemental_001": TWO}

        ground = combat._check_blockers(engine, TWO, [
            {"creature_id": "grizzly_bears_001", "blocking_id": "air_elemental_001"}])
        self.assertIn("flying", ground)

        flyer = combat._check_blockers(engine, TWO, [
            {"creature_id": "ornithopter_001", "blocking_id": "air_elemental_001"}])
        self.assertIsNone(flyer)

    def test_one_creature_cannot_block_two_attackers(self):
        engine, state = make_engine()
        put(state, ONE, "grizzly_bears_001")
        put(state, ONE, "grizzly_bears_002")
        put(state, TWO, "savannah_lions_001")
        state.attackers = {"grizzly_bears_001": TWO, "grizzly_bears_002": TWO}

        problem = combat._check_blockers(engine, TWO, [
            {"creature_id": "savannah_lions_001", "blocking_id": "grizzly_bears_001"},
            {"creature_id": "savannah_lions_001", "blocking_id": "grizzly_bears_002"},
        ])
        self.assertIn("more than one attacker", problem)

    def test_summoning_sick_creature_may_still_block(self):
        """Summoning sickness stops attacking and tap abilities, not blocking."""
        engine, state = make_engine()
        put(state, ONE, "grizzly_bears_001")
        put(state, TWO, "savannah_lions_001", summoning_sick=True)
        state.attackers = {"grizzly_bears_001": TWO}

        self.assertIsNone(combat._check_blockers(engine, TWO, [
            {"creature_id": "savannah_lions_001", "blocking_id": "grizzly_bears_001"}]))


class DeclarationApplicationTests(unittest.TestCase):
    """The declaration steps, driven with scripted client replies."""

    def declare(self, state, attackers):
        engine = ScriptedEngine(state, [
            {"type": protocol.DECLARE_ATTACKERS, "seq_num": 5, "attackers": attackers}])
        combat.declare_attackers(engine, 5)
        return engine

    def test_declaring_an_attacker_taps_it(self):
        """Declaring an attacker taps it immediately (RFC Section 9.3)."""
        engine, state = make_engine()
        bears = put(state, ONE, "grizzly_bears_001")
        self.declare(state, [{"creature_id": "grizzly_bears_001", "target": TWO}])

        self.assertTrue(bears.tapped)
        self.assertEqual(state.attackers, {"grizzly_bears_001": TWO})

    def test_vigilance_attacker_does_not_tap(self):
        """Serra Angel has vigilance, so attacking leaves it untapped."""
        engine, state = make_engine()
        angel = put(state, ONE, "serra_angel_001")
        self.declare(state, [{"creature_id": "serra_angel_001", "target": TWO}])

        self.assertFalse(angel.tapped)
        self.assertIn("serra_angel_001", state.attackers)

    def test_empty_declaration_means_no_attack(self):
        engine, state = make_engine()
        put(state, ONE, "grizzly_bears_001")
        self.declare(state, [])
        self.assertEqual(state.attackers, {})

    def test_blocking_does_not_tap_the_blockers(self):
        engine, state = make_engine()
        put(state, ONE, "grizzly_bears_001")
        blocker = put(state, TWO, "savannah_lions_001")
        state.attackers = {"grizzly_bears_001": TWO}

        engine = ScriptedEngine(state, [{
            "type": protocol.DECLARE_BLOCKERS, "seq_num": 9,
            "blockers": [{"creature_id": "savannah_lions_001",
                          "blocking_id": "grizzly_bears_001"}]}])
        combat.declare_blockers(engine, 9)

        self.assertFalse(blocker.tapped)
        self.assertEqual(state.blocks, {"grizzly_bears_001": ["savannah_lions_001"]})

    def test_assign_damage_order_validates_then_records(self):
        """One ASSIGN_DAMAGE_ORDER per multiply-blocked attacker (RFC 9.5)."""
        engine, state = make_engine()
        put(state, ONE, "leatherback_baloth_001")
        put(state, TWO, "grizzly_bears_001")
        put(state, TWO, "savannah_lions_001")
        state.attackers = {"leatherback_baloth_001": TWO}
        state.blocks = {"leatherback_baloth_001":
                        ["grizzly_bears_001", "savannah_lions_001"]}

        engine = ScriptedEngine(state, [
            # An attacker that is not awaiting an order.
            {"type": protocol.ASSIGN_DAMAGE_ORDER, "seq_num": 11,
             "attacker_id": "grizzly_bears_001", "blocker_order": []},
            # Not a permutation of that attacker's blockers.
            {"type": protocol.ASSIGN_DAMAGE_ORDER, "seq_num": 11,
             "attacker_id": "leatherback_baloth_001",
             "blocker_order": ["grizzly_bears_001"]},
            # Valid.
            {"type": protocol.ASSIGN_DAMAGE_ORDER, "seq_num": 11,
             "attacker_id": "leatherback_baloth_001",
             "blocker_order": ["savannah_lions_001", "grizzly_bears_001"]},
        ])
        combat.assign_damage_orders(engine, ["leatherback_baloth_001"], 11)

        self.assertEqual(state.damage_order,
                         {"leatherback_baloth_001":
                          ["savannah_lions_001", "grizzly_bears_001"]})
        self.assertEqual([code for code, _ in engine.errors],
                         [protocol.ILLEGAL_ACTION, protocol.ILLEGAL_ACTION])

    def test_damage_follows_the_chosen_order_end_to_end(self):
        """The recorded order drives the actual damage assignment."""
        engine, state = make_engine()
        put(state, ONE, "leatherback_baloth_001")    # 4/5
        bears = put(state, TWO, "grizzly_bears_001")   # 2/2
        lions = put(state, TWO, "savannah_lions_001")  # 2/1
        state.attackers = {"leatherback_baloth_001": TWO}
        state.blocks = {"leatherback_baloth_001":
                        ["grizzly_bears_001", "savannah_lions_001"]}
        state.damage_order = {"leatherback_baloth_001":
                              ["savannah_lions_001", "grizzly_bears_001"]}

        combat.deal_combat_damage(engine, first_strike_step=False)

        # Lions took 1 (lethal), Bears took the remaining 3; both died, and the
        # attacker took 2 + 2 back but survives on 5 toughness.
        self.assertIsNone(state.find_permanent("savannah_lions_001"))
        self.assertIsNone(state.find_permanent("grizzly_bears_001"))
        attacker = state.find_permanent("leatherback_baloth_001")
        self.assertIsNotNone(attacker)
        self.assertEqual(attacker.damage, 4)
        self.assertEqual(state.player(TWO).life, 20)


# --- State-based actions (RFC Section 8.4) -----------------------------

class StateBasedActionTests(unittest.TestCase):

    def test_lethal_damage_destroys_a_creature(self):
        engine, state = make_engine()
        bears = put(state, ONE, "grizzly_bears_001")
        bears.damage = 2

        died = engine.check_state_based_actions()
        self.assertEqual(died, ["grizzly_bears_001"])
        self.assertIn("grizzly_bears_001", state.player(ONE).graveyard)

    def test_damage_below_toughness_is_survivable(self):
        engine, state = make_engine()
        wall = put(state, ONE, "wall_of_stone_001")   # 0/8
        wall.damage = 7
        self.assertEqual(engine.check_state_based_actions(), [])
        self.assertIsNotNone(state.find_permanent("wall_of_stone_001"))

    def test_zero_life_loses_the_game(self):
        engine, state = make_engine()
        state.player(TWO).life = 0

        with self.assertRaises(GameOver) as caught:
            engine.check_state_based_actions()
        self.assertEqual(caught.exception.reason, protocol.REASON_LIFE_ZERO)
        self.assertEqual(caught.exception.winner_id, ONE)
        self.assertEqual(caught.exception.loser_id, TWO)

    def test_simultaneous_death_loses_for_the_active_player(self):
        """If both players hit zero at once, the Active Player loses."""
        engine, state = make_engine()
        state.active_player = ONE
        state.player(ONE).life = -1
        state.player(TWO).life = 0

        with self.assertRaises(GameOver) as caught:
            engine.check_state_based_actions()
        self.assertEqual(caught.exception.loser_id, ONE)
        self.assertEqual(caught.exception.winner_id, TWO)


# --- Mana payment (RFC Section 7.5) ------------------------------------

class ManaTests(unittest.TestCase):

    def test_a_mountain_pays_one_red(self):
        engine, state = make_engine()
        mountain = put(state, ONE, "mountain_001")
        tapped = mana.pay(state.player(ONE), {"R": 1})
        self.assertEqual(tapped, [mountain])
        self.assertTrue(mountain.tapped)

    def test_wrong_colour_cannot_pay(self):
        engine, state = make_engine()
        put(state, ONE, "forest_001")
        with self.assertRaises(mana.InsufficientMana):
            mana.pay(state.player(ONE), {"R": 1})

    def test_generic_mana_accepts_any_colour(self):
        engine, state = make_engine()
        put(state, ONE, "forest_001")
        put(state, ONE, "island_001")
        tapped = mana.pay(state.player(ONE), {"X": 2})
        self.assertEqual(len(tapped), 2)

    def test_sol_ring_pays_two_generic_with_one_tap(self):
        engine, state = make_engine()
        ring = put(state, ONE, "sol_ring_001")
        tapped = mana.pay(state.player(ONE), {"X": 2})
        self.assertEqual(tapped, [ring])

    def test_colored_requirements_are_funded_before_generic(self):
        """Searing Spear needs {1}{R}: the Mountain must go to the red pip."""
        engine, state = make_engine()
        put(state, ONE, "mountain_001")
        put(state, ONE, "forest_001")
        tapped = mana.pay(state.player(ONE), {"R": 1, "X": 1})
        self.assertEqual(len(tapped), 2)

    def test_tapped_and_summoning_sick_sources_are_unavailable(self):
        engine, state = make_engine()
        put(state, ONE, "forest_001", tapped=True)
        # A creature with summoning sickness cannot use a tap ability for mana.
        put(state, ONE, "llanowar_elves_001", summoning_sick=True)
        with self.assertRaises(mana.InsufficientMana):
            mana.pay(state.player(ONE), {"G": 1})

        # Once the sickness wears off, the Elves can be tapped.
        state.player(ONE).battlefield[1].summoning_sick = False
        self.assertEqual(len(mana.pay(state.player(ONE), {"G": 1})), 1)

    def test_nothing_is_tapped_when_the_payment_cannot_be_met(self):
        """Payment is atomic: a failure must leave the battlefield untouched."""
        engine, state = make_engine()
        mountain = put(state, ONE, "mountain_001")
        with self.assertRaises(mana.InsufficientMana):
            mana.pay(state.player(ONE), {"R": 1, "X": 3})
        self.assertFalse(mountain.tapped)

    def test_declared_payment_must_equal_the_printed_cost(self):
        bolt = cards.lookup("lightning_bolt_001")
        mana.check_matches_cost({"R": 1}, bolt.cost)          # Exact: fine.
        with self.assertRaises(mana.InsufficientMana):
            mana.check_matches_cost({}, bolt.cost)
        with self.assertRaises(mana.InsufficientMana):
            mana.check_matches_cost({"G": 1}, bolt.cost)
        with self.assertRaises(mana.InsufficientMana):
            mana.check_matches_cost({"R": 2}, bolt.cost)

    def test_unknown_mana_symbol_is_rejected(self):
        with self.assertRaises(mana.InsufficientMana):
            mana.normalise({"Q": 1})


# --- Card effects ------------------------------------------------------

class EffectTests(unittest.TestCase):

    def resolve(self, engine, card_id, controller, targets):
        """Push a spell and resolve it, returning its state_changes."""
        item = StackItem(
            stack_item_id=engine.state.next_stack_item_id(),
            item_type=protocol.ITEM_SPELL, source=card_id,
            controller=controller, targets=list(targets))
        engine.state.stack.append(item)
        card = cards.lookup(card_id)
        effect = effects.spell_effect_for(card)
        return effect(engine.state, item)

    def test_lightning_bolt_deals_three_to_a_player(self):
        engine, state = make_engine()
        changes = self.resolve(engine, "lightning_bolt_001", ONE, [TWO])
        self.assertEqual(state.player(TWO).life, 17)
        self.assertEqual(changes[0]["change_type"], "DAMAGE")
        self.assertEqual(changes[0]["amount"], 3)

    def test_flame_slash_marks_damage_on_a_creature(self):
        engine, state = make_engine()
        wall = put(state, TWO, "wall_of_stone_001")     # 0/8 survives 4 damage
        self.resolve(engine, "flame_slash_001", ONE, ["wall_of_stone_001"])
        self.assertEqual(wall.damage, 4)
        self.assertEqual(engine.check_state_based_actions(), [])

    def test_giant_growth_lasts_until_end_of_turn(self):
        engine, state = make_engine()
        bears = put(state, ONE, "grizzly_bears_001")
        self.resolve(engine, "giant_growth_001", ONE, ["grizzly_bears_001"])
        self.assertEqual((bears.power, bears.toughness), (5, 5))

    def test_doom_blade_cannot_target_a_black_creature(self):
        engine, state = make_engine()
        put(state, TWO, "gray_merchant_001")        # black
        put(state, TWO, "grizzly_bears_001")        # green
        spec = effects.SPELL_EFFECTS["doom_blade"][0]

        self.assertFalse(effects.is_legal_target(state, spec, "gray_merchant_001", ONE))
        self.assertTrue(effects.is_legal_target(state, spec, "grizzly_bears_001", ONE))

    def test_swords_to_plowshares_exiles_and_gives_life(self):
        engine, state = make_engine()
        put(state, TWO, "leatherback_baloth_001")   # power 4
        self.resolve(engine, "swords_to_plowshares_001", ONE, ["leatherback_baloth_001"])

        self.assertIsNone(state.find_permanent("leatherback_baloth_001"))
        self.assertIn("leatherback_baloth_001", state.player(TWO).exile)
        # Its controller -- the opponent -- gains life equal to its power.
        self.assertEqual(state.player(TWO).life, 24)

    def test_unsummon_returns_a_creature_to_its_owners_hand(self):
        engine, state = make_engine()
        put(state, TWO, "grizzly_bears_001")
        self.resolve(engine, "unsummon_001", ONE, ["grizzly_bears_001"])
        self.assertIn("grizzly_bears_001", state.player(TWO).hand)
        self.assertIsNone(state.find_permanent("grizzly_bears_001"))

    def test_counterspell_removes_a_spell_from_the_stack(self):
        engine, state = make_engine()
        victim = StackItem(stack_item_id="stk_99", item_type=protocol.ITEM_SPELL,
                           source="grizzly_bears_001", controller=TWO)
        state.stack.append(victim)

        changes = effects.counter_spell(state, "stk_99")
        self.assertEqual(state.stack, [])
        self.assertIn("grizzly_bears_001", state.player(TWO).graveyard)
        self.assertEqual(changes[0]["change_type"], "COUNTER")

    def test_negate_cannot_counter_a_creature_spell(self):
        engine, state = make_engine()
        state.stack.append(StackItem(
            stack_item_id="stk_01", item_type=protocol.ITEM_SPELL,
            source="grizzly_bears_001", controller=TWO))
        state.stack.append(StackItem(
            stack_item_id="stk_02", item_type=protocol.ITEM_SPELL,
            source="lightning_bolt_001", controller=TWO))

        spec = effects.SPELL_EFFECTS["negate"][0]
        self.assertFalse(effects.is_legal_target(state, spec, "stk_01", ONE))
        self.assertTrue(effects.is_legal_target(state, spec, "stk_02", ONE))

    def test_raise_dead_only_sees_your_own_graveyard(self):
        engine, state = make_engine()
        state.player(ONE).graveyard.append("grizzly_bears_001")
        state.player(TWO).graveyard.append("savannah_lions_001")
        spec = effects.SPELL_EFFECTS["raise_dead"][0]

        self.assertTrue(effects.is_legal_target(state, spec, "grizzly_bears_001", ONE))
        self.assertFalse(effects.is_legal_target(state, spec, "savannah_lions_001", ONE))
        # A land in the graveyard is not a creature card.
        state.player(ONE).graveyard.append("mountain_001")
        self.assertFalse(effects.is_legal_target(state, spec, "mountain_001", ONE))

    def test_devotion_to_black_counts_black_mana_symbols(self):
        engine, state = make_engine()
        put(state, ONE, "gray_merchant_001")    # costs {3}{B}{B} -> 2 symbols
        put(state, ONE, "black_knight_001")     # costs {B}{B}     -> 2 symbols
        put(state, ONE, "mountain_001")         # no black symbols
        self.assertEqual(effects.devotion_to_black(state, ONE), 4)

    def test_gray_merchant_drains_and_gains(self):
        engine, state = make_engine()
        put(state, ONE, "gray_merchant_001")
        item = StackItem(stack_item_id="stk_01",
                         item_type=protocol.ITEM_TRIGGER_ABILITY,
                         source="gray_merchant_001", controller=ONE,
                         trigger_key="gray_merchant", payload={"amount": 2})
        effects.ALL_TRIGGERS["gray_merchant"].effect(state, item)

        self.assertEqual(state.player(TWO).life, 18)
        self.assertEqual(state.player(ONE).life, 22)

    def test_cards_without_an_implemented_effect_are_not_castable(self):
        """Pacifism is an Aura; attachments are out of scope for this build."""
        self.assertIsNone(effects.target_spec_for_spell(cards.lookup("pacifism_001")))
        self.assertIsNone(effects.target_spec_for_spell(cards.lookup("ponder_001")))
        # Permanents need no implemented effect: they simply enter the battlefield.
        self.assertEqual(effects.target_spec_for_spell(cards.lookup("grizzly_bears_001")),
                         effects.NO_TARGET)


# --- Stack resolution and fizzling (RFC Section 8.4) ------------------

class StackResolutionTests(unittest.TestCase):

    def test_spell_fizzles_when_its_only_target_is_gone(self):
        engine, state = make_engine()
        put(state, TWO, "grizzly_bears_001")
        state.stack.append(StackItem(
            stack_item_id="stk_01", item_type=protocol.ITEM_SPELL,
            source="flame_slash_001", controller=ONE,
            targets=["grizzly_bears_001"]))

        # The target leaves the battlefield before the spell resolves.
        state.player(TWO).battlefield.clear()
        priority.resolve_top_of_stack(engine)

        self.assertEqual(state.stack, [])
        # A fizzled sorcery still goes to its owner's graveyard.
        self.assertIn("flame_slash_001", state.player(ONE).graveyard)
        self.assertEqual(state.player(TWO).life, 20)

    def test_creature_spell_becomes_a_summoning_sick_permanent(self):
        engine, state = make_engine()
        state.stack.append(StackItem(
            stack_item_id="stk_01", item_type=protocol.ITEM_SPELL,
            source="grizzly_bears_001", controller=ONE))

        priority.resolve_top_of_stack(engine)
        permanent = state.find_permanent("grizzly_bears_001")
        self.assertIsNotNone(permanent)
        self.assertTrue(permanent.summoning_sick)
        self.assertFalse(permanent.tapped)

    def test_instant_goes_to_the_graveyard_after_resolving(self):
        engine, state = make_engine()
        state.stack.append(StackItem(
            stack_item_id="stk_01", item_type=protocol.ITEM_SPELL,
            source="lightning_bolt_001", controller=ONE, targets=[TWO]))

        priority.resolve_top_of_stack(engine)
        self.assertEqual(state.player(TWO).life, 17)
        self.assertIn("lightning_bolt_001", state.player(ONE).graveyard)

    def test_lethal_resolution_ends_the_game_at_once(self):
        engine, state = make_engine()
        state.player(TWO).life = 3
        state.stack.append(StackItem(
            stack_item_id="stk_01", item_type=protocol.ITEM_SPELL,
            source="lightning_bolt_001", controller=ONE, targets=[TWO]))

        with self.assertRaises(GameOver) as caught:
            priority.resolve_top_of_stack(engine)
        self.assertEqual(caught.exception.reason, protocol.REASON_LIFE_ZERO)
        self.assertEqual(caught.exception.winner_id, ONE)

    def test_stack_is_last_in_first_out(self):
        engine, state = make_engine()
        for index, card_id in enumerate(("lightning_bolt_001", "shock_001"), start=1):
            state.stack.append(StackItem(
                stack_item_id=f"stk_{index:02d}", item_type=protocol.ITEM_SPELL,
                source=card_id, controller=ONE, targets=[TWO]))

        # Shock was added last, so it resolves first: 20 - 2 = 18.
        priority.resolve_top_of_stack(engine)
        self.assertEqual(state.player(TWO).life, 18)
        priority.resolve_top_of_stack(engine)
        self.assertEqual(state.player(TWO).life, 15)


# --- Visible state filtering (RFC Section 4.2) ------------------------

class VisibleStateTests(unittest.TestCase):

    def test_each_player_sees_only_their_own_hand(self):
        engine, state = make_engine()
        state.player(ONE).hand = ["lightning_bolt_001", "mountain_001"]
        state.player(TWO).hand = ["counterspell_001"]
        state.player(ONE).library = ["shock_001"] * 5

        view = state.visible_state(ONE)
        self.assertEqual(view["hand"], {ONE: ["lightning_bolt_001", "mountain_001"]})
        self.assertEqual(view["hand_counts"], {TWO: 1})
        self.assertNotIn(TWO, view["hand"])
        # Libraries are counts only, never contents.
        self.assertEqual(view["library_counts"][ONE], 5)
        self.assertNotIn("library", view)

    def test_creature_entries_carry_combat_details(self):
        engine, state = make_engine()
        bears = put(state, ONE, "grizzly_bears_001")
        bears.damage = 1
        put(state, ONE, "mountain_001")

        entries = {e["id"]: e for e in state.visible_state(ONE)["battlefield"][ONE]}
        self.assertEqual(entries["grizzly_bears_001"]["power"], 2)
        self.assertEqual(entries["grizzly_bears_001"]["damage"], 1)
        self.assertIn("summoning_sick", entries["grizzly_bears_001"])
        # A non-creature carries only its id and tapped state.
        self.assertEqual(set(entries["mountain_001"]), {"id", "tapped"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
