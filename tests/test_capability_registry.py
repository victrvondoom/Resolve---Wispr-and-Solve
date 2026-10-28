import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from capability_registry import WORKSPACES, concepts_for_workspace, load_registry


COMPUTER_NAMES = '''PLVA
Handyman
Reel-Learn
Kylian
Patchwork
Snowglobe
Otto
Occasion
Ivy
Halo
Glassbox
BuyWith402
FollowThrough
CUA Molecular/Proteome Visualizer
Life-Admin Operator
cloud_CUA
Nativas AI
ImproveMe
HomeAgent
Holodev
Voodoo
Talksy
Workfleet
Voyager
Hiking Route Agent
Screenloom
HoloMobile
AutoPilot-AI
Holo Forge
ai runbooks
A-Test
Maison H
ApartmentAgent
AI Teaching Assistant
EnterpriseOS
GrannySmith
Meta.B
HOUSTON
GeoWave
Space Control Delta
ArcFlash Copilot
Claw-DJ
Agent kalshi
simulation-labs'''.splitlines()
GTM_NAMES = '''Gravity
Zorro
Net-Work
Re:lay
FOMO AI
Sentinel
Knock Knock
AlphaLuma
Telum
Hiresweet
SabreChord
Revibe
Polar
Ouija
Sonar
Modjo Team BDR Prospector 3000
SSA Platform
Tourelle
TSC
Before You Quit
Eos
LOCUS
PipeWork
Growth Protocol
Operator-less
Madeleine
Litmus
OMO
1:1 Marketing at Scale
Closing AGENT
GTM Event
Tindent
Revive
Cupidon
VELV
1er Signal'''.splitlines()


class RegistryTests(unittest.TestCase):
    def test_exact_80_names_and_source_split(self):
        entries = load_registry()
        self.assertEqual(len(entries), 80)
        self.assertEqual([item['name'] for item in entries[:44]], COMPUTER_NAMES)
        self.assertEqual([item['name'] for item in entries[44:]], GTM_NAMES)
        self.assertEqual({item['implementation_status'] for item in entries}, {'PLANNED'})
        self.assertEqual({item['test_status'] for item in entries}, {'PLANNED'})
        self.assertEqual({item['demo_status'] for item in entries}, {'PLANNED'})
        self.assertTrue(all(item['original_purpose'] is None for item in entries))

    def test_all_ten_workspaces_have_routed_concepts(self):
        self.assertEqual(len(WORKSPACES), 10)
        counts = {name: len(concepts_for_workspace(name)) for name in WORKSPACES}
        self.assertTrue(all(count > 0 for count in counts.values()), counts)
        self.assertEqual(sum(counts.values()), 80)

    def test_validator_rejects_false_verified_claim(self):
        entries = load_registry()
        entries[0]['test_status'] = 'VERIFIED'
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'invalid.json'
            path.write_text(json.dumps(entries), encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'planned implementation cannot be marked tested'):
                load_registry(path)

    def test_validator_rejects_duplicate_name(self):
        entries = load_registry()
        entries[1]['name'] = entries[0]['name']
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'invalid.json'
            path.write_text(json.dumps(entries), encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'duplicate concept name'):
                load_registry(path)


if __name__ == '__main__':
    unittest.main()
