"""
A change of price source, end to end through server/card_data.py (a Codex review of 2026-10-24): the repricing of both
inventories runs under one lock with the change itself, and a change that could not finish is remembered and finished by
the next repricing - a failed one never leaves numbers of the old currency (or of another shop) shown in the new one.
Temporary databases (test_ownership.TemporaryData); nothing is downloaded and no application data is touched.

    venv/bin/python -m unittest discover tests -p test_price_source_change.py
"""
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app as launcher  # noqa: E402,F401 - the server's modules
from card_scanner.server import lock  # noqa: E402
from card_scanner.server.state import app  # noqa: E402
from card_scanner.station.station_login import Login  # noqa: E402
from test_ownership import GAME  # noqa: E402
from test_prices import BOTH, ETCHED, FILLER, CardData, scryfall_card  # noqa: E402

card_data = sys.modules['card_scanner.server.card_data']
BASE = 'http://localhost'


class FakeSettings(dict):
    """data/settings.json in memory: get / set as app_settings has them - and always true, as settings.Settings is (a dict
    is false while empty, and `if app_settings:` would then skip what the server does)"""

    failing = ()        # keys whose durable write fails (a disk that refuses the settings file)

    def set(self, key, value):
        self[key] = value

    def set_durably(self, key, value):
        if key in self.failing:
            raise OSError(f'cannot write settings.json ({key})')
        self[key] = value

    def __bool__(self):
        return True


class SourceChange(CardData):
    def setUp(self):
        super().setUp()
        # EURO_ONLY: Cardmarket has a price, TCGplayer none (the case of the second review: a euro number left as dollars)
        self.cards.populate_database(FILLER + [ETCHED, BOTH, scryfall_card(4, {'eur': '2.00'})])
        self.settings = FakeSettings()
        add = lambda card_id, finish: self.collection.add_card(
            self.game.inventory_fields(self.cards.get_card_by_id(card_id), finish), GAME, finish, quiet=True)
        self.both, self.etched = add('id-2', 'regular'), add('id-1', 'foil')     # the etched one has no euro price
        self.scanned = self.scan.add_card(self.game.inventory_fields(self.cards.get_card_by_id('id-1'), 'foil'), GAME, 'foil', quiet=True)
        login = Login(file=Path(tempfile.mkdtemp()) / 'settings_login.json', variable='TEST_SETTINGS_PASSWORD',
                      cookie=lock.COOKIE, session_seconds=lock.SESSION_SECONDS)
        for patch in (mock.patch.object(card_data, 'app_settings', self.settings),
                      mock.patch.object(card_data, 'database', self.cards),
                      mock.patch.object(card_data, 'inventory', self.collection),
                      mock.patch.object(card_data, 'scan_inventory', self.scan),
                      mock.patch.object(card_data.games, 'active', lambda: self.game),
                      mock.patch.object(card_data, 'socketio', mock.Mock()),
                      mock.patch.object(lock, 'login', login),
                      mock.patch.object(card_data.logger, 'exception')):     # the failures these tests cause on purpose
            patch.start()
            self.addCleanup(patch.stop)
        # As startup.py sets them: an inventory a half-done change has not repriced shows no prices
        self.collection.prices_withheld = lambda: card_data.prices_withheld('collection')
        self.scan.prices_withheld = lambda: card_data.prices_withheld('scan')
        self.collection.current_price = self.scan.current_price = card_data.current_card_price
        for patch in (mock.patch.object(card_data.games, 'get', lambda game_id: self.game if game_id == GAME else None),):
            patch.start()
            self.addCleanup(patch.stop)
        self.client = app.test_client()

    def choose(self, source):
        return self.client.post('/api/prices/source', json={'source': source}, base_url=BASE)

    def price(self, store, row_id):
        return store.get_entry(row_id)['price_usd']

    def test_a_change_finishes_and_is_forgotten(self):
        answer = self.choose('scryfall_eur')
        self.assertEqual(answer.status_code, 200)
        self.assertEqual(answer.get_json()['unpriced'], 2)                 # the etched copy in each inventory
        self.assertEqual((self.price(self.collection, self.both), self.price(self.collection, self.etched)), (0.9, None))
        self.assertIsNone(card_data.price_transition())

    def test_a_failed_change_is_answered_and_finished_by_the_next_repricing(self):
        # The second inventory fails half-way: the collection is in euros, the scanned cards still hold dollars
        with mock.patch.object(self.scan, 'reprice', side_effect=OSError('disk full')):
            answer = self.choose('scryfall_eur')
        self.assertEqual(answer.status_code, 500)                           # never a success with dollars left as euros
        self.assertEqual(self.cards.price_source, 'scryfall_eur')
        self.assertEqual(card_data.price_transition(), ['scryfall_usd', 'scryfall_eur'])    # remembered, both
        self.assertEqual(self.price(self.scan, self.scanned), 4.5)         # a dollar number, now shown as euros ...
        # ... until the next repricing of any kind (here: what a card data update or a start runs)
        self.assertEqual(card_data.reprice_collections(self.game), (0, 1))
        self.assertIsNone(self.price(self.scan, self.scanned))
        self.assertIsNone(card_data.price_transition())

    def test_a_half_done_change_shows_no_old_currency(self):
        # The scanned cards could not be repriced: until they are, they show no price and a total of 0 - never their
        # dollars as euros (a Codex review of 2026-10-24); the collection, repriced, shows its euros
        with mock.patch.object(self.scan, 'reprice', side_effect=OSError('disk full')):
            self.assertEqual(self.choose('scryfall_eur').status_code, 500)
        self.assertEqual(self.scan.get_entry(self.scanned)['price_usd'], 4.5)       # still dollars underneath ...
        stats = self.scan.get_stats(GAME)
        self.assertEqual((stats['total_value'], stats['prices_pending']), (0, True))     # ... never shown
        self.assertEqual(self.scan.get_stats(GAME, station=None)['total_value'], 0)
        self.assertTrue(all(card['price'] == 0 for card in self.scan.get_all_cards(GAME)))
        collection = self.collection.get_stats(GAME)
        self.assertEqual((collection['total_value'], collection['prices_pending']), (0.9, False))
        # The schedule's tick finishes it (every 10 minutes): the scanned card loses its dollars, the prices are shown again
        card_data.finish_unfinished_repricing()
        self.assertIsNone(self.scan.get_entry(self.scanned)['price_usd'])
        self.assertFalse(self.scan.get_stats(GAME)['prices_pending'])
        self.assertIsNone(card_data.price_transition())

    def test_a_total_read_while_the_change_finishes_is_never_old_as_current(self):
        # Half-done: the scanned cards hold $4.50, withheld. A read's rows are fetched; the retry finishes; only THEN did
        # the read ask "withheld?" (no) - and gave the old dollars as the new total (a Codex review of 2026-10-24). Asked
        # under the same lock as the rows, the retry waits for the read instead
        with mock.patch.object(self.scan, 'reprice', side_effect=OSError('disk full')):
            self.assertEqual(self.choose('scryfall_eur').status_code, 500)
        real = self.scan.prices_withheld
        retry = []

        def finishing_meanwhile():
            if not retry:
                retry.append(threading.Thread(target=card_data.finish_unfinished_repricing))
                retry[0].start()
                retry[0].join(0.3)                       # the retry gets every chance to run here
            return real()
        self.scan.prices_withheld = finishing_meanwhile
        stats = self.scan.get_stats(GAME)
        retry[0].join(5)
        self.assertEqual(stats['total_value'], 0)        # withheld, or the new total - never the old $4.50
        self.scan.prices_withheld = real
        self.assertEqual(self.scan.get_stats(GAME)['prices_pending'], False)

    def test_a_change_in_the_same_currency_withholds_nothing(self):
        self.cards.replace_shop_prices({'id-1': {'cardkingdom_etched': 6.0}, **{f'id-{n}': {'cardkingdom': 0.2} for n in range(100, 140)}},
                                       '2026-10-24')
        with mock.patch.object(self.scan, 'reprice', side_effect=OSError('disk full')):
            self.choose('cardkingdom_usd')
        self.assertFalse(self.scan.get_stats(GAME)['prices_pending'])            # dollars either way

    def test_an_inventory_is_repriced_whole_or_not_at_all(self):
        # Its new prices and the ones taken away in one transaction: a failure in the second part leaves the first undone
        with mock.patch.object(self.scan, 'clear_prices', side_effect=OSError('disk full')):
            self.choose('scryfall_eur')
        pending = self.settings[card_data.PRICE_TRANSITION_KEY]
        self.assertEqual(pending['done'], ['collection'])

    def test_choosing_it_again_finishes_it_too(self):
        with mock.patch.object(self.scan, 'clear_prices', side_effect=OSError('disk full')):
            self.assertEqual(self.choose('scryfall_eur').status_code, 500)
        answer = self.choose('scryfall_eur')                               # the same source: nothing new to remember
        self.assertEqual(answer.status_code, 200)
        self.assertEqual(answer.get_json()['unpriced'], 1)                 # the scanned card's, left by the failure
        self.assertIsNone(card_data.price_transition())

    def test_back_to_where_it_came_from_leaves_nothing_cleared(self):
        with mock.patch.object(self.scan, 'reprice', side_effect=OSError('disk full')):
            self.choose('scryfall_eur')
        answer = self.choose('scryfall_usd')
        self.assertEqual(answer.get_json()['unpriced'], 0)                 # every entry has its dollar price
        self.assertEqual(self.price(self.collection, self.etched), 4.5)    # its dollar price back
        self.assertEqual(self.price(self.scan, self.scanned), 4.5)
        self.assertIsNone(card_data.price_transition())

    def test_a_euro_number_of_a_half_done_change_never_stays_as_dollars(self):
        # The collection is repriced in euros, the scanned cards fail; back to dollars: a printing TCGplayer has no price
        # for holds Cardmarket's euros - it must lose them, never keep them as dollars (a Codex review of 2026-10-24)
        euro_only = self.collection.add_card(self.game.inventory_fields(self.cards.get_card_by_id('id-4'), 'regular'),
                                             GAME, 'regular', quiet=True)
        with mock.patch.object(self.scan, 'reprice', side_effect=OSError('disk full')):
            self.choose('scryfall_eur')
        self.assertEqual(self.price(self.collection, euro_only), 2.0)      # euros, written before the failure
        self.assertEqual(self.choose('scryfall_usd').status_code, 200)
        self.assertIsNone(self.price(self.collection, euro_only))
        self.assertIsNone(card_data.price_transition())

    def test_the_same_through_a_third_source_in_dollars(self):
        euro_only = self.collection.add_card(self.game.inventory_fields(self.cards.get_card_by_id('id-4'), 'regular'),
                                             GAME, 'regular', quiet=True)
        self.cards.replace_shop_prices({f'id-{n}': {'manapool': 0.2} for n in range(100, 140)}, '2026-10-24')
        with mock.patch.object(self.scan, 'reprice', side_effect=OSError('disk full')):
            self.choose('scryfall_eur')
        self.assertEqual(self.choose('manapool_usd').status_code, 200)
        self.assertIsNone(self.price(self.collection, euro_only))
        self.assertIsNone(card_data.price_transition())

    def test_a_change_through_a_third_source_keeps_the_first_currency(self):
        # Euros failed half-way, then Card Kingdom: compared with where the unfinished change came from (dollars), not
        # with the euros shown meanwhile - the same currency, so the scanned card's dollar price is fine to replace
        self.cards.replace_shop_prices({'id-1': {'cardkingdom_etched': 6.0}, **{f'id-{n}': {'cardkingdom': 0.2} for n in range(100, 140)}},
                                       '2026-10-24')
        with mock.patch.object(self.scan, 'reprice', side_effect=OSError('disk full')):
            self.choose('scryfall_eur')
        self.assertEqual(self.choose('cardkingdom_usd').status_code, 200)
        self.assertEqual(self.price(self.scan, self.scanned), 6.0)
        self.assertIsNone(self.price(self.collection, self.both))           # Card Kingdom has no price for it
        self.assertIsNone(card_data.price_transition())

    def test_a_repricing_works_out_and_writes_under_the_lock(self):
        # A change of source cannot come between the prices worked out and their writing (a Codex review of 2026-10-24)
        seen = []
        real = self.game.price_changes

        def watched(*args, **kwargs):
            outside = threading.Thread(target=lambda: seen.append(card_data.pricing_lock.acquire(blocking=False)))
            outside.start()
            outside.join()
            return real(*args, **kwargs)
        with mock.patch.object(self.game, 'price_changes', watched):
            card_data.reprice_collections(self.game)
        self.assertEqual(seen, [False, False])                              # another thread could not take it, twice

    def test_a_shop_update_finishes_an_unfinished_change(self):
        with mock.patch.object(self.scan, 'reprice', side_effect=OSError('disk full')):
            self.choose('scryfall_eur')
        done = threading.Event()
        with mock.patch.object(card_data.shop_prices, 'import_prices', return_value=('2026-10-24', 0)):
            card_data.start_shop_price_update(on_done=lambda ok, message: done.set())
            self.assertTrue(done.wait(5))
        self.assertIsNone(card_data.price_transition())
        self.assertIsNone(self.price(self.scan, self.scanned))

    def shop_update(self, outcome):
        """One update of the shops' prices, import_prices answering `outcome` (a value, or an exception to raise); (ok, message)"""
        ended = []
        done = threading.Event()
        effect = {'side_effect': outcome} if isinstance(outcome, Exception) else {'return_value': outcome}
        with mock.patch.object(card_data.shop_prices, 'import_prices', **effect):
            card_data.start_shop_price_update(on_done=lambda ok, message: (ended.append((ok, message)), done.set()))
            self.assertTrue(done.wait(5))
        return ended[0]

    def test_a_failed_repricing_after_new_shop_prices_is_a_failed_update_and_tried_again(self):
        # Card Kingdom shown; new prices come in but the scanned cards cannot be priced again. The update says so, and
        # the next one - although MTGJSON has nothing newer - prices them (a Codex review of 2026-10-24)
        self.cards.replace_shop_prices({'id-1': {'cardkingdom_etched': 6.0}, **{f'id-{n}': {'cardkingdom': 0.2} for n in range(100, 140)}},
                                       '2026-10-24')
        self.assertEqual(self.choose('cardkingdom_usd').status_code, 200)
        self.cards.replace_shop_prices({'id-1': {'cardkingdom_etched': 7.0}, **{f'id-{n}': {'cardkingdom': 0.2} for n in range(100, 140)}},
                                       '2026-10-25')                      # what import_prices would have swapped in
        with mock.patch.object(self.scan, 'reprice', side_effect=OSError('disk full')):
            ok, message = self.shop_update(('2026-10-25', 41))
        self.assertFalse(ok)
        self.assertIn('could not be priced again', message)
        self.assertTrue(card_data.reprice_pending())
        self.assertEqual(self.price(self.scan, self.scanned), 6.0)        # yesterday's
        ok, message = self.shop_update(card_data.shop_prices.NothingNew('The prices of 2026-10-25 are the ones in use'))
        self.assertTrue(ok)
        self.assertEqual(self.price(self.scan, self.scanned), 7.0)
        self.assertFalse(card_data.reprice_pending())

    def test_nothing_new_and_nothing_pending_reprices_nothing(self):
        with mock.patch.object(card_data, 'reprice_collections') as reprice:
            self.assertTrue(self.shop_update(card_data.shop_prices.NothingNew('same'))[0])
        reprice.assert_not_called()

    def test_a_start_reprices_what_a_failure_left(self):
        # A change stopped while it ran (a restart in the middle of a download): counted as done, priced again once
        self.settings[card_data.REPRICE_PENDING_KEY] = {'stopped': 'running'}
        ran = []
        with mock.patch.object(card_data.threading, 'Thread', lambda target, args, **kwargs: mock.Mock(start=lambda: ran.append(target(*args)))):
            card_data.resume_price_transition()
        self.assertEqual(len(ran), 1)
        self.assertFalse(card_data.reprice_pending())
        self.assertEqual(self.settings[card_data.REPRICE_PENDING_KEY], {})

    def test_a_change_of_source_during_a_download_does_not_swallow_its_repricing(self):
        # The download is slow; meanwhile the person switches to Card Kingdom (repriced from yesterday's shop table). Then
        # the new day's prices are swapped in: both inventories must take them (a Codex review of 2026-10-24 - a single
        # "pending" flag was cleared by the switch's repricing, and the update's own repricing skipped)
        yesterday = {'id-1': {'cardkingdom_etched': 6.0}, **{f'id-{n}': {'cardkingdom': 0.2} for n in range(100, 140)}}
        today = {'id-1': {'cardkingdom_etched': 7.0}, 'id-2': {'cardkingdom': 1.5}, **{f'id-{n}': {'cardkingdom': 0.2} for n in range(100, 140)}}
        self.cards.replace_shop_prices(yesterday, '2026-10-24')

        def slow_import(database, progress, force=False):
            self.assertEqual(self.choose('cardkingdom_usd').status_code, 200)    # during the download
            self.assertEqual(self.price(self.scan, self.scanned), 6.0)          # repriced from yesterday's table
            database.replace_shop_prices(today, '2026-10-25')                  # then the swap
            return '2026-10-25', len(today)
        done = threading.Event()
        with mock.patch.object(card_data.shop_prices, 'import_prices', slow_import):
            card_data.start_shop_price_update(on_done=lambda ok, message: done.set())
            self.assertTrue(done.wait(5))
        self.assertEqual(self.price(self.scan, self.scanned), 7.0)
        self.assertEqual(self.price(self.collection, self.etched), 7.0)
        self.assertEqual(self.price(self.collection, self.both), 1.5)
        self.assertFalse(card_data.reprice_pending())

    def test_the_same_from_a_scryfall_source(self):
        # Started under TCGplayer (nothing to reprice at its start), Card Kingdom chosen during the download
        self.cards.replace_shop_prices({'id-1': {'cardkingdom_etched': 6.0}, **{f'id-{n}': {'cardkingdom': 0.2} for n in range(100, 140)}},
                                       '2026-10-24')

        def slow_import(database, progress, force=False):
            self.choose('cardkingdom_usd')
            database.replace_shop_prices({'id-1': {'cardkingdom_etched': 8.0}, **{f'id-{n}': {'cardkingdom': 0.2} for n in range(100, 140)}},
                                         '2026-10-25')
            return '2026-10-25', 41
        done = threading.Event()
        with mock.patch.object(card_data.shop_prices, 'import_prices', slow_import):
            card_data.start_shop_price_update(on_done=lambda ok, message: done.set())
            self.assertTrue(done.wait(5))
        self.assertEqual(self.price(self.scan, self.scanned), 8.0)

    def test_scanned_cards_added_to_the_collection_mid_pass_wait_for_it(self):
        # Dollars to euros; between the collection's pass and the scanned cards' pass, "Add to collection" moves the
        # scanned etched copy (a dollar price, none in euros) into the collection - priced already, so it would keep its
        # dollars, shown as euros (a Codex review of 2026-10-24). The move must wait for the whole pass
        from card_scanner.server.state import maintenance_barrier
        moved = threading.Event()
        mover = []
        real = self.game.price_changes

        def between_the_passes(store, *args, **kwargs):
            if store is self.scan and not mover:
                def add_to_collection():
                    with maintenance_barrier:                                  # as inventory_api's route takes it
                        self.collection.take_from(self.scan, GAME)
                    moved.set()
                mover.append(threading.Thread(target=add_to_collection))
                mover[0].start()
                self.assertFalse(moved.wait(0.3), 'the move went in between the two passes')
            return real(store, *args, **kwargs)
        with mock.patch.object(self.game, 'price_changes', between_the_passes):
            self.assertEqual(self.choose('scryfall_eur').status_code, 200)
        mover[0].join(5)
        self.assertTrue(moved.is_set())
        self.assertEqual(self.scan.get_stats(GAME)['total_cards'], 0)
        etched = [entry for entry in self.collection.get_all_cards(GAME) if entry['card_id'] == 'id-1']
        self.assertTrue(etched)
        self.assertTrue(all(not entry['price'] for entry in etched), etched)   # no dollar number left as euros
        self.assertEqual(self.collection.get_stats(GAME)['total_value'], 0.9)   # BOTH's euros only

    def test_a_card_read_before_a_change_is_added_at_the_new_price(self):
        # The scanner page shows a card - its price read in dollars - and the person adds it after a change to euros
        # (minutes later, or in the middle of the change): written at the card data's price at that moment, never in
        # dollars under the € sign (a Codex review of 2026-10-24)
        shown = self.game.inventory_fields(self.cards.get_card_by_id('id-2'), 'foil')       # $3.00, read now
        self.assertEqual(shown['price'], 3.0)
        self.assertEqual(self.choose('scryfall_eur').status_code, 200)
        row_id = self.scan.add_card(shown, GAME, 'foil', quiet=True)
        self.assertEqual(self.price(self.scan, row_id), 2.5)

    def test_an_edit_whose_price_was_read_before_a_change(self):
        # update_inventory_card reads the new finish's price, then calls update_card: a change between the two
        finish_price = self.game.inventory_fields(self.cards.get_card_by_id('id-2'), 'foil')['price']   # $3.00
        read, go_on, edited = threading.Event(), threading.Event(), []

        def edit():
            read.set()
            go_on.wait(5)
            edited.append(self.collection.update_card(self.both, finish='foil', finish_price=finish_price, quiet=True))
        worker = threading.Thread(target=edit)
        worker.start()
        read.wait(5)
        self.assertEqual(self.choose('scryfall_eur').status_code, 200)       # while the edit holds its old price
        go_on.set()
        worker.join(5)
        self.assertTrue(edited and edited[0]['success'])
        foil = [entry for entry in self.collection.get_all_cards(GAME) if entry['card_id'] == 'id-2' and entry['finish'] == 'foil']
        self.assertEqual([entry['price'] for entry in foil], [2.5])

    def no_quote_today(self):
        """A new card data download in which Card 2 has no price at all (a gap of the day): the entries keep theirs"""
        self.cards.populate_database(FILLER + [ETCHED, {**BOTH, 'prices': {}}, scryfall_card(4, {'eur': '2.00'})])
        card_data.reprice_collections(self.game)
        self.assertEqual(self.game.current_price('id-2', 'regular'), 0.0)

    def test_moved_to_the_collection_a_card_keeps_its_price_on_a_day_without_a_quote(self):
        # A Codex review of 2026-10-24: "Add to collection" wrote the day's 0 over the scanned card's price
        scanned = self.scan.add_card(self.game.inventory_fields(self.cards.get_card_by_id('id-2'), 'regular'), GAME, 'regular',
                                     quiet=True, location='Box')
        self.no_quote_today()
        self.assertEqual(self.price(self.scan, scanned), 1.0)                  # kept by the repricing ...
        self.collection.take_from(self.scan, GAME)
        moved = [e for e in self.collection.get_all_cards(GAME) if e['card_id'] == 'id-2' and e['location'] == 'Box']
        self.assertEqual([e['price'] for e in moved], [1.0])                    # ... and by the move

    def test_copies_moved_to_another_location_keep_their_price_too(self):
        self.collection.update_card(self.both, quantity=2, quiet=True)
        self.no_quote_today()
        self.collection.update_card(self.both, location='Binder', split_quantity=1, quiet=True)
        prices = sorted(e['price'] for e in self.collection.get_all_cards(GAME) if e['card_id'] == 'id-2')
        self.assertEqual(prices, [1.0, 1.0])

    def test_a_new_entry_without_a_quote_is_0(self):
        # Not kept from what its caller read: that may be a price of before a change of source
        shown = self.game.inventory_fields(self.cards.get_card_by_id('id-2'), 'regular')     # $1.00, read before
        self.no_quote_today()
        row_id = self.collection.add_card(shown, GAME, 'regular', quiet=True, location='New')
        self.assertEqual(self.price(self.collection, row_id), 0.0)

    def test_a_withheld_inventory_carries_no_old_currency_into_the_collection(self):
        # Dollars to euros fails on the scanned cards; then "Add to collection": a card without its printing and one the
        # card data lacks have no euro quote - they arrive without a price, never with their dollars under the € sign
        unknown = {'card_id': None, 'name': 'Card 9', 'set_name': 'Test', 'set_code': 'tst', 'number': '9', 'price': 7.0}
        missing = {'card_id': 'not-in-the-data', 'name': 'Card 8', 'set_name': 'Test', 'set_code': 'tst', 'number': '8', 'price': 5.0}
        for fields in (unknown, missing):
            self.scan.add_card(fields, GAME, 'regular', quiet=True)
        with mock.patch.object(self.scan, 'reprice', side_effect=OSError('disk full')):
            self.assertEqual(self.choose('scryfall_eur').status_code, 500)
        self.assertTrue(self.scan.prices_withheld())
        self.collection.take_from(self.scan, GAME)
        moved = {e['name']: e for e in self.collection.get_all_cards(GAME) if e['name'] in ('Card 9', 'Card 8', 'Card 1')}
        self.assertEqual({name: e['price'] for name, e in moved.items()}, {'Card 9': 0.0, 'Card 8': 0.0, 'Card 1': 0.0})
        self.assertEqual(self.collection.get_stats(GAME)['total_value'], 0.9)    # BOTH's euros, nothing else

    def test_copies_split_off_during_a_repricing_wait_for_it(self):
        # Dollars to euros; between the collection's entries read and their prices written, two etched copies (no euro
        # price) are split off to another location: the split must wait, or its new row keeps the dollars (a Codex review
        # of 2026-10-24)
        self.collection.update_card(self.etched, quantity=2, quiet=True)
        split, done = [], threading.Event()
        real = self.game.price_changes

        def read_then_split(store, *args, **kwargs):
            answer = real(store, *args, **kwargs)
            if store is self.collection and not split:
                def move():
                    split.append(self.collection.update_card(self.etched, location='Binder', split_quantity=1, quiet=True))
                    done.set()
                threading.Thread(target=move).start()
                self.assertFalse(done.wait(0.3), 'the split went in between the read and the write')
            return answer
        with mock.patch.object(self.game, 'price_changes', read_then_split):
            self.assertEqual(self.choose('scryfall_eur').status_code, 200)
        self.assertTrue(done.wait(5))
        etched = [e['price'] for e in self.collection.get_all_cards(GAME) if e['card_id'] == 'id-1']
        self.assertEqual(sorted(etched), [0.0, 0.0])                      # both rows without a price: no dollars left

    def test_no_trade_is_confirmed_while_the_collection_prices_are_withheld(self):
        # The collection's repricing fails during dollars -> euros: a trade confirmed now would record 0 for its cards
        # for good (a Codex review of 2026-10-24) - refused, and confirmed at its real value once repriced
        from card_scanner.core.trades import TradeError, Trades
        trades = Trades(self.collection)
        trade = trades.add(GAME, 'Jo', [self.both])['trade']
        with mock.patch.object(self.collection, 'reprice', side_effect=OSError('disk full')):
            self.assertEqual(self.choose('scryfall_eur').status_code, 500)
        self.assertTrue(self.collection.prices_withheld())
        with self.assertRaises(TradeError):
            trades.confirm(trade)
        self.assertEqual(self.collection.get_entry(self.both)['quantity'], 1)           # nothing left the collection
        card_data.finish_unfinished_repricing()
        result = trades.confirm(trade)
        self.assertEqual(result['value'], 0.9)                                          # its euro value, recorded

    def test_an_unknown_printing_keeps_the_price_it_came_with(self):
        # A CSV row of a printing the card data lacks: no current price to take
        row_id = self.collection.add_card({'card_id': 'not-in-the-data', 'name': 'Card 9', 'set_name': 'Test', 'set_code': 'tst',
                                           'number': '9', 'price': 7.0}, GAME, 'regular', quiet=True)
        self.assertEqual(self.price(self.collection, row_id), 7.0)

    def test_a_note_that_cannot_be_saved_stops_the_change(self):
        # The change of source cannot be noted on disk: nothing may change - a restart would not know of it (a Codex review
        # of 2026-10-24: the settings file's errors were printed and forgotten)
        self.settings.failing = (card_data.PRICE_TRANSITION_KEY,)
        answer = self.choose('scryfall_eur')
        self.assertEqual(answer.status_code, 500)
        self.assertEqual(self.cards.price_source, 'scryfall_usd')
        self.assertEqual(self.cards.get_card_by_id('id-2')['price'], 1.0)
        self.assertEqual(self.price(self.collection, self.both), 1.0)

    def test_a_source_that_cannot_be_saved_is_undone(self):
        # The prices are in, the choice cannot be saved: the prices go back - else a restart shows the old source over them
        self.settings.failing = ('price_source',)
        answer = self.choose('scryfall_eur')
        self.assertEqual(answer.status_code, 500)
        self.assertEqual(self.cards.price_source, 'scryfall_usd')
        self.assertEqual(self.cards.get_card_by_id('id-2')['price'], 1.0)
        self.assertIsNone(card_data.price_transition())
        self.assertEqual(self.price(self.collection, self.both), 1.0)

    def test_an_entry_written_before_the_save_fails_is_repriced_back(self):
        # The UPDATE is in (euros shown), a card is added at its euro price, then the choice cannot be saved: going back
        # to dollars must take that card back to dollars too (a Codex review of 2026-10-24: the rollback restored the
        # card data's prices and forgot the change, and the card kept its euros as dollars)
        added = []
        real = self.settings.set_durably

        def add_then_fail(key, value):
            if key == 'price_source':
                fields = self.game.inventory_fields(self.cards.get_card_by_id('id-2'), 'foil')
                added.append(self.scan.add_card(fields, GAME, 'foil', quiet=True))
                raise OSError('cannot write settings.json')
            return real(key, value)
        self.settings.set_durably = add_then_fail
        answer = self.choose('scryfall_eur')
        self.assertEqual(answer.status_code, 500)
        self.assertEqual(self.cards.price_source, 'scryfall_usd')
        self.assertEqual(self.price(self.scan, added[0]), 3.0)                 # written at €2.50, back to $3.00
        self.assertIsNone(card_data.price_transition())

    def test_a_price_update_that_cannot_be_noted_does_not_begin(self):
        self.settings.failing = (card_data.REPRICE_PENDING_KEY,)
        with mock.patch.object(card_data.shop_prices, 'import_prices') as imported:
            ok, message = self.shop_update(None)
        self.assertFalse(ok)
        imported.assert_not_called()

    def test_a_change_whose_update_fails_changes_nothing(self):
        # The source, its setting and the change's note stay as they were (a Codex review of 2026-10-24: the choice was
        # recorded before its UPDATE, and a failed one left the new source over the old prices)
        real = self.cards.conn
        self.cards.conn = FailingConnection(real)
        try:
            answer = self.choose('scryfall_eur')
        finally:
            self.cards.conn = real
        self.assertEqual(answer.status_code, 500)
        self.assertEqual(self.cards.price_source, 'scryfall_usd')
        self.assertEqual(self.cards.get_card_by_id('id-2')['price'], 1.0)      # still dollars, and shown as dollars
        self.assertNotIn('price_source', self.settings)
        self.assertIsNone(card_data.price_transition())
        self.assertEqual(self.price(self.collection, self.both), 1.0)

    def test_a_failed_download_leaves_nothing_to_follow(self):
        ok, message = self.shop_update(OSError('no network'))
        self.assertFalse(ok)
        self.assertEqual(self.settings.get(card_data.REPRICE_PENDING_KEY), {})

    def test_a_start_finishes_it(self):
        self.settings[card_data.PRICE_TRANSITION_KEY] = {'sources': ['scryfall_usd', 'scryfall_eur']}
        self.cards.set_price_source('scryfall_eur')
        started = []
        with mock.patch.object(card_data.threading, 'Thread', lambda target, args, **kwargs: mock.Mock(start=lambda: started.append(target(*args)))):
            card_data.resume_price_transition()
        self.assertEqual(started, [(1, 2)])                                 # both etched copies cleared, BOTH repriced
        self.assertIsNone(card_data.price_transition())


class FailingConnection:
    """The card data's connection, with every UPDATE failing (the file locked past SQLite's timeout)"""

    def __init__(self, conn):
        self.real = conn

    def execute(self, sql, *args):
        if sql.lstrip().upper().startswith('UPDATE'):
            raise RuntimeError('database is locked')
        return self.real.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self.real, name)


class NoLockCycle(CardData):
    """An inventory write asks the card data for a price while it holds SQLite's write lock on the same file (the collection
    lives in the card database's file); a change of price source holds the card data's lock while its UPDATE waits for that
    write lock. The price must be read without that lock, or each waits for the other until SQLite's timeout (a Codex review
    of 2026-10-24)"""

    def test_a_price_is_read_while_a_change_waits_for_the_write_lock(self):
        import time
        from card_scanner.core.inventory import InventoryManager
        self.cards.populate_database(FILLER + [BOTH])
        same_file = InventoryManager(db_file=self.tmp / 'cards.db')
        self.addCleanup(same_file.conn.close)
        same_file.conn.execute('BEGIN IMMEDIATE')                  # an inventory write under way: SQLite's write lock held
        changed = []
        change = threading.Thread(target=lambda: changed.append(self.cards.set_price_source('scryfall_eur')))
        change.start()
        time.sleep(0.3)                                            # the change holds the card data's lock, its UPDATE waiting
        started = time.monotonic()
        self.assertEqual(self.game.current_price('id-2', 'regular'), 1.0)     # the dollars still shown: the change waits
        self.assertLess(time.monotonic() - started, 2, 'the price waited for the card data lock')
        same_file.conn.commit()                                    # the write ends; the change goes on
        change.join(15)
        self.assertEqual(changed, ['scryfall_eur'])
        self.assertEqual(self.game.current_price('id-2', 'regular'), 0.9)


class DurableSettings(unittest.TestCase):
    """settings.Settings.set_durably: a failed write is raised, and the value in memory put back"""

    def test_a_failed_write_raises_and_changes_nothing(self):
        import os
        from card_scanner import settings as settings_module
        folder = Path(tempfile.mkdtemp())
        with mock.patch.object(settings_module.Settings, 'SETTINGS_FILE', folder / 'settings.json'):
            stored = settings_module.Settings()
            stored.set('price_source', 'scryfall_usd')
            os.chmod(folder, 0o500)                              # the folder refuses new files (the write goes beside it)
            self.addCleanup(os.chmod, folder, 0o700)
            if os.access(folder, os.W_OK):
                self.skipTest('running as a user who can write anywhere')
            with self.assertRaises(OSError):
                stored.set_durably('price_source', 'scryfall_eur')
            self.assertEqual(stored.get('price_source'), 'scryfall_usd')
            import contextlib
            import io
            printed = io.StringIO()
            with contextlib.redirect_stdout(printed):
                stored.set('price_source', 'x')                  # the old set: printed and forgotten, as before
            self.assertEqual(stored.get('price_source'), 'x')
            self.assertIn('Error saving settings', printed.getvalue())


class ReapplyUnderTheLock(CardData):
    """CardDatabase.reapply_price_source reads the choice under the lock set_price_source changes it under"""

    def test_a_shop_update_never_undoes_a_change(self):
        self.cards.populate_database(FILLER + [BOTH])
        self.cards.set_price_source('scryfall_eur')
        self.assertFalse(self.cards.reapply_price_source(kind='shops'))     # Cardmarket is not a shop's source: untouched
        self.assertEqual(self.cards.get_card_by_id('id-2')['price'], 0.9)
        self.assertFalse(self.cards.reapply_price_source(unless='scryfall_eur'))
        self.assertTrue(self.cards.reapply_price_source(unless='scryfall_usd'))


if __name__ == '__main__':
    unittest.main()
