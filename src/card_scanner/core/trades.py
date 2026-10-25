# ============================================================================
# FILE: trades.py
# Cards set aside for a trade: they stay in the collection (still owned, still
# counted) until the trade is confirmed - only then are they removed - or it is
# cancelled. The tables and the triggers that keep the copies are in
# inventory.py (TRADES_TABLE, TRADE_CARDS_TABLE, TRADE_TRIGGERS).
# ============================================================================
import hashlib
import json

from card_scanner.core.inventory import _row_dict, now


class TradeError(Exception):
    """What the page shows as it is"""


class Trades:
    """
    The collection's trades. An open trade's rows name the entries and how many of their copies
    are set aside (several trades can hold copies of one entry, never more between them than
    it has); the database refuses to delete such an entry or to lower it below that. A
    confirmed trade is history: its rows keep the cards as they were (JSON), no entry.
    """

    def __init__(self, inventory):
        self.inventory = inventory
        self.conn = inventory.conn
        self._lock = inventory._lock

    def _writing(self, change):
        """Run a change as one edit of the collection: committed whole, or rolled back"""
        with self.inventory._transaction():
            return change()

    def _trade(self, trade_id, status='open'):
        row = self.conn.execute('SELECT * FROM trades WHERE id = ?', (trade_id,)).fetchone()
        if not row or row['status'] != status:
            raise TradeError('This trade is not open any more' if status == 'open' else 'Trade not found')
        return row

    def _set_aside(self, entry_id, except_row=None):
        """How many of an entry's copies the open trades hold (without one of their rows)"""
        return self.conn.execute('SELECT COALESCE(SUM(quantity), 0) FROM trade_cards WHERE inventory_id = ? AND id IS NOT ?',
                                 (entry_id, except_row)).fetchone()[0]

    # ------------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------------

    def list(self, game):
        """
        A game's trades, the open ones first, then the confirmed ones (newest first):
        {'id', 'name', 'status' ('open' / 'done'), 'created_at', 'closed_at', 'cards': [...],
        'quantity', 'value', and what it receives: 'receives' (receives()), 'receive_quantity',
        'receive_value'}. A card is the inventory row as the page and the exporters know it,
        with 'quantity' the copies in the trade, 'row' (the trade's row, to change it) and - in
        an open trade - 'entry_quantity' and 'most' (how many the trade could hold).
        """
        with self._lock:
            trades = [dict(row) for row in self.conn.execute(
                "SELECT * FROM trades WHERE game = ? ORDER BY status = 'done', COALESCE(closed_at, created_at) DESC, id DESC",
                (game,))]
            for trade in trades:
                trade['cards'] = self.cards(trade['id'])
                trade['quantity'] = sum(card['quantity'] for card in trade['cards'])
                trade['value'] = round(sum(card['price'] * card['quantity'] for card in trade['cards']), 2)
                # What it receives (someone else's cards, receive())
                trade['receives'] = self.receives(trade['id'])
                trade['receive_quantity'] = sum(card['quantity'] for card in trade['receives'])
                trade['receive_value'] = round(sum(card['price'] * card['quantity'] for card in trade['receives']), 2)
                # What an open trade holds, as one string: a confirm sends the one its dialog was shown with (confirm)
                trade['fingerprint'] = self.fingerprint(trade['id']) if trade['status'] == 'open' else None
        return trades

    def cards(self, trade_id):
        """A trade's cards (see list), by name"""
        with self._lock:
            cards = []
            for row in self.conn.execute('''
                    SELECT c.id AS trade_row, c.quantity AS copies, c.card, i.* FROM trade_cards c
                    LEFT JOIN inventory i ON i.id = c.inventory_id WHERE c.trade_id = ?''', (trade_id,)).fetchall():
                if row['id'] is None:  # confirmed: the card as it was
                    card = {**json.loads(row['card']), 'row': row['trade_row'], 'quantity': row['copies']}
                else:
                    card = {**_row_dict(row, withheld=self.inventory.prices_withheld()), 'row': row['trade_row'], 'quantity': row['copies'],
                            'entry_quantity': row['quantity'],
                            'most': row['quantity'] - self._set_aside(row['id'], row['trade_row'])}
                cards.append(card)
        return sorted(cards, key=lambda card: (card['name'].lower(), card['set_name'], card['number']))

    def receives(self, trade_id):
        """The cards a trade receives, as they were put in (card_dict of core/others.py, with 'row' -
        the trade's row -, 'quantity' here, 'most' the other person has, 'seller' whose)"""
        with self._lock:
            return [{**json.loads(row['card']), 'row': row['id'], 'quantity': row['quantity'], 'most': row['most'],
                     'seller': row['source']} for row in self.conn.execute(
                'SELECT * FROM trade_receives WHERE trade_id = ? ORDER BY source COLLATE NOCASE, id', (trade_id,))]

    def fingerprint(self, trade_id):
        """
        A digest of exactly what an open trade holds: every card it gives - its row, entry, copies and the entry's
        identity (printing, name, set, number, finish, condition, location: an edit of the entry changes the card given) -
        and every card it receives (row, seller, card, copies). Two moments with the same digest hold the same cards; totals
        alone do not say that (one card swapped for another, copies moved between rows - a Codex finding). The price is
        left out: a repricing changes it every day without changing what is given, and would refuse confirms for nothing.
        """
        with self._lock:
            gives = self.conn.execute(
                "SELECT c.id, c.inventory_id, c.quantity, i.card_id, i.card_name, i.set_name, i.card_number, i.finish, "
                "i.condition, i.location FROM trade_cards c LEFT JOIN inventory i ON i.id = c.inventory_id "
                "WHERE c.trade_id = ? ORDER BY c.id", (trade_id,)).fetchall()
            receives = self.conn.execute("SELECT id, source, key, quantity FROM trade_receives WHERE trade_id = ? ORDER BY id",
                                         (trade_id,)).fetchall()
        content = json.dumps([[list(row) for row in gives], [list(row) for row in receives]])
        return hashlib.sha256(content.encode()).hexdigest()[:32]

    def by_entry(self, game):
        """{inventory id: [{'id', 'name', 'quantity'}]}: the open trades holding copies of each entry"""
        result = {}
        with self._lock:
            for row in self.conn.execute('''
                    SELECT c.inventory_id, c.quantity, t.id, t.name FROM trade_cards c JOIN trades t ON t.id = c.trade_id
                    WHERE t.game = ? AND c.inventory_id IS NOT NULL ORDER BY t.id''', (game,)):
                result.setdefault(row['inventory_id'], []).append(
                    {'id': row['id'], 'name': row['name'], 'quantity': row['quantity']})
        return result

    # ------------------------------------------------------------------------
    # Changing
    # ------------------------------------------------------------------------

    def _open_trade(self, game, name):
        """The open trade with this name (any case), made when there is none: (id, its name, made now)"""
        name = ' '.join(str(name or '').split())[:60]
        if not name:
            raise TradeError('The trade needs a name')
        trade = self.conn.execute("SELECT id, name FROM trades WHERE game = ? AND status = 'open' AND name = ? COLLATE NOCASE",
                                  (game, name)).fetchone()
        if trade:
            return trade['id'], trade['name'], False
        return self.conn.execute("INSERT INTO trades (game, name, status, created_at) VALUES (?, ?, 'open', ?)",
                                 (game, name, now())).lastrowid, name, True

    def receive(self, game, name, source, cards):
        """
        Cards of someone else's collection (source: its name) that the open trade with this name
        receives - made when there is none; it can receive without giving anything. cards:
        [{'card' (card_dict of core/others.py), 'key', 'quantity', 'most'}]; the copies add to
        what the trade receives of that card from that source, never above `most` (what the other
        person has). Nothing of the collection is touched. Returns {'trade', 'name', 'cards'}.
        """
        def change():
            # Cards of another game are not put in this game's trade (a page left open while the
            # game was switched): the snapshot says whose game it is (core/others.py: card_dict)
            if any(item['card'].get('game') != game for item in cards):
                raise TradeError('These cards are of another game than the trade')
            trade_id, trade_name, _made = self._open_trade(game, name)
            copies = 0
            held = 'SELECT quantity FROM trade_receives WHERE trade_id = ? AND source = ? AND key = ?'
            for item in cards:
                quantity = max(1, int(item['quantity']))
                most = max(1, int(item['most']))
                before = self.conn.execute(held, (trade_id, source, item['key'])).fetchone()
                self.conn.execute('''
                    INSERT INTO trade_receives (trade_id, source, key, card, quantity, most) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(trade_id, source, key) DO UPDATE SET
                        quantity = MIN(quantity + excluded.quantity, excluded.most), most = excluded.most''',
                                  (trade_id, source, item['key'], json.dumps(item['card']), min(quantity, most), most))
                # What the row gained - the copies are cut at what the other person has, so it can be less than asked
                after = self.conn.execute(held, (trade_id, source, item['key'])).fetchone()
                copies += after['quantity'] - (before['quantity'] if before else 0)
            return {'trade': trade_id, 'name': trade_name, 'cards': copies}
        return self._writing(change)

    def set_receive_quantity(self, trade_id, row_id, quantity):
        """How many copies of one received card an open trade holds; 0 takes the card out"""
        def change():
            self._trade(trade_id)
            row = self.conn.execute('SELECT * FROM trade_receives WHERE id = ? AND trade_id = ?', (row_id, trade_id)).fetchone()
            if not row:
                raise TradeError('This card is not in the trade any more')
            if quantity <= 0:
                self.conn.execute('DELETE FROM trade_receives WHERE id = ?', (row_id,))
            elif quantity > row['most']:
                raise TradeError(f"{row['source']} has only {row['most']} of it")
            else:
                self.conn.execute('UPDATE trade_receives SET quantity = ? WHERE id = ?', (quantity, row_id))
        return self._writing(change)

    def add(self, game, name, entry_ids):
        """
        Set entries aside for the open trade with this name (made when there is none): every
        copy of each that no trade holds yet. Returns {'trade': id, 'name', 'cards': copies set
        aside now, 'skipped': entries that had none free}.
        """
        name = ' '.join(str(name or '').split())[:60]
        if not name:
            raise TradeError('The trade needs a name')

        def change():
            trade_id, trade_name, made = self._open_trade(game, name)
            cards = skipped = 0
            for entry_id in dict.fromkeys(entry_ids):
                entry = self.inventory._get_row(entry_id)
                free = entry['quantity'] - self._set_aside(entry_id) if entry and entry['game'] == game else 0
                if free <= 0:
                    skipped += 1
                    continue
                self.conn.execute('''INSERT INTO trade_cards (trade_id, inventory_id, quantity) VALUES (?, ?, ?)
                                     ON CONFLICT(trade_id, inventory_id) DO UPDATE SET quantity = quantity + excluded.quantity''',
                                  (trade_id, entry_id, free))
                cards += free
            if not cards and made:
                raise TradeError('None of these cards has a copy that is not in a trade already')
            return {'trade': trade_id, 'name': trade_name, 'cards': cards, 'skipped': skipped}
        return self._writing(change)

    def set_quantity(self, trade_id, row_id, quantity):
        """How many copies of one of its cards an open trade holds; 0 takes the card out"""
        def change():
            self._trade(trade_id)
            row = self.conn.execute('SELECT * FROM trade_cards WHERE id = ? AND trade_id = ?', (row_id, trade_id)).fetchone()
            if not row:
                raise TradeError('This card is not in the trade any more')
            if quantity <= 0:
                self.conn.execute('DELETE FROM trade_cards WHERE id = ?', (row_id,))
                return
            entry = self.inventory._get_row(row['inventory_id'])
            most = entry['quantity'] - self._set_aside(entry['id'], row_id)
            if quantity > most:
                raise TradeError(f"Only {most} of {entry['card_name']} can go into this trade")
            self.conn.execute('UPDATE trade_cards SET quantity = ? WHERE id = ?', (quantity, row_id))
        return self._writing(change)

    def rename(self, trade_id, name):
        name = ' '.join(str(name or '').split())[:60]
        if not name:
            raise TradeError('The trade needs a name')

        def change():
            trade = self.conn.execute('SELECT * FROM trades WHERE id = ?', (trade_id,)).fetchone()
            if not trade:
                raise TradeError('Trade not found')
            if trade['status'] == 'open' and self.conn.execute(
                    "SELECT 1 FROM trades WHERE game = ? AND status = 'open' AND name = ? COLLATE NOCASE AND id != ?",
                    (trade['game'], name, trade_id)).fetchone():
                raise TradeError(f'There is an open trade called "{name}" already')
            self.conn.execute('UPDATE trades SET name = ? WHERE id = ?', (name, trade_id))
        return self._writing(change)

    def _status(self, trade_id):
        row = self.conn.execute('SELECT status FROM trades WHERE id = ?', (trade_id,)).fetchone()
        if not row:
            raise TradeError('Trade not found')
        return row['status']

    def cancel(self, trade_id):
        """The trade is off: its cards are simply the collection's again. Returns the copies freed. A confirmed trade is
        refused, never deleted: a page still showing it as open (another tab, the phone) must not take its record away"""
        def change():
            if self._status(trade_id) != 'open':
                raise TradeError('This trade was confirmed meanwhile: it stays in the history of confirmed trades')
            freed = self.conn.execute('SELECT COALESCE(SUM(quantity), 0) FROM trade_cards WHERE trade_id = ?',
                                      (trade_id,)).fetchone()[0]
            self.conn.execute('DELETE FROM trades WHERE id = ?', (trade_id,))  # its rows go with it
            return freed
        return self._writing(change)

    def confirm(self, trade_id, expected=None):
        """
        The trade happened: its copies leave the collection (the newest captures and the
        stations' counts with them, as when a quantity is lowered) and the trade stays as a
        record of the cards as they were. One transaction. Returns {'cards', 'entries', 'value'}.
        expected: what the person was shown when they confirmed - {'fingerprint'} (list(), fingerprint()), or from a page
        of before it {'quantity', 'receive_quantity'} - refused when the trade holds anything else now (changed in another
        tab meanwhile): what leaves must be what was agreed to. Compared inside this transaction, under the lock every
        change of a trade takes, so nothing can change between the check and the copies leaving.
        """
        def change():
            self._trade(trade_id)
            if expected is not None:
                if 'fingerprint' in expected:
                    changed = expected['fingerprint'] != self.fingerprint(trade_id)
                else:   # a page from before the fingerprint: its totals, which miss a swap of cards
                    now_held = [self.conn.execute(f'SELECT COALESCE(SUM(quantity), 0) FROM {table} WHERE trade_id = ?',
                                                  (trade_id,)).fetchone()[0] for table in ('trade_cards', 'trade_receives')]
                    changed = now_held != [expected.get('quantity'), expected.get('receive_quantity')]
                if changed:
                    raise TradeError('This trade changed since it was shown: look at it again and confirm once more')
            manager = self.inventory
            if manager.prices_withheld():
                # A change of price source half-done (InventoryManager.prices_withheld): the record would keep 0 for every
                # card, for good - it no longer refers to the entries a repricing corrects (a Codex review of 2026-10-24).
                # Checked inside the collection's transaction and lock, which the repricing holds too
                raise TradeError('The prices are being changed to another currency: confirm the trade once they are '
                                 'repriced (a few minutes at most)')
            cards = entries = 0
            value = 0.0
            for row in self.conn.execute('SELECT * FROM trade_cards WHERE trade_id = ?', (trade_id,)).fetchall():
                entry = manager._get_row(row['inventory_id'])
                card = {**_row_dict(entry, withheld=manager.prices_withheld()), 'quantity': row['quantity']}
                # No entry any more: the copies are free to go (TRADE_TRIGGERS)
                self.conn.execute('UPDATE trade_cards SET inventory_id = NULL, card = ? WHERE id = ?',
                                  (json.dumps(card), row['id']))
                left = entry['quantity'] - row['quantity']
                manager._take_copies(entry['id'], row['quantity'])
                if left > 0:
                    manager._trim_sources(entry['id'], left)
                    manager._trim_captures(entry['id'], left)
                cards += row['quantity']
                entries += 1
                value += card['price'] * row['quantity']
            self.conn.execute("UPDATE trades SET status = 'done', closed_at = ? WHERE id = ?", (now(), trade_id))
            manager.last_added = {}  # an undo must not reach into entries that changed under it
            return {'cards': cards, 'entries': entries, 'value': round(value, 2)}
        result = self._writing(change)
        self.inventory.log(f"Trade confirmed: {result['cards']} cards left the collection", level="success")
        return result

    def delete(self, trade_id):
        """Take a confirmed trade out of the history (an open one is cancelled instead: cancel)"""
        def change():
            if self._status(trade_id) == 'open':
                raise TradeError('This trade is open: cancel it instead - only a confirmed trade is deleted from the history')
            self.conn.execute('DELETE FROM trades WHERE id = ?', (trade_id,))
        return self._writing(change)
