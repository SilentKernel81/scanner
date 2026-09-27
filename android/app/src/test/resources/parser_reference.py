import json, sys, logging
from difflib import SequenceMatcher
sys.path.insert(0,'.')
from card_identifier import CardIdentifier
ci = CardIdentifier.__new__(CardIdentifier); ci.log_callback=None
cases = ["NAME: Lightning Bolt\nNUMBER: 0141\nSET: M11", "- **NAME**: Riolu (The card is blue)\n- **NUMBER**: 018B\n- **SET**: HOB EN",
 "Mirkwood / 0188 / HOB", "Mirkwood\n0188\nHOB", "Mirkwood\nNUMBER: 0188\nSET: HOB", "NAME: Unknown\nNUMBER: Unknown\nSET: Unknown",
 "NAME: Pikachu\nNUMBER: 016 / 131 ★\nSET: Unknown", "Sol Ring\nL 018B", "NAME: Arwen\r\nNUMBER: 12\r\nSET: LTR", "I cannot read this card."]
res = [ci._parse_response(c, 'x') for c in cases]
pairs = [("thands","thanos, the mad titan"),("fili","fíli"),("lightning bolt","lightning blot"),("abcd","bcda"),("mirkwood","mirkwood trapper")]
print(json.dumps({"cases":cases,"parsed":res,"pairs":pairs,"ratios":[SequenceMatcher(None,a,b).ratio() for a,b in pairs]}))
