import copy
import json
import unittest
from student import treatment, features, Scorer, with_public_history


def fixture(screen='REWARDS'):
    return {'public_state':{'screen':screen,'persistent_resources':{'hp':50,'deck':[{'id':1,'upgraded':False}]},
                'visible_act_boss':'HEXAGHOST','map_graph':{'nodes':[{'x':1,'y':0,'room_symbol':'M'}],
                'edges':[{'from_x':1,'to_x':2,'from_y':0,'to_y':1}]},
                'current_map_node':{'x':1,'y':0},'screen_context':{'coverage_status':'supported'}},
            'legal_options':[{'candidate_index':i,'action':{'kind':'reward_card' if screen=='REWARDS' else 'map',
                'label':'SECRET_OFFER_'+str(i),'idx1':i},'semantics':{'card':{'id':i+5,'upgraded':False},
                'destination':{'x':i,'y':1,'room_symbol':'M'}}} for i in range(3)]}


class StudentTests(unittest.TestCase):
    def test_changed_content_visible_distinct_masked_identical(self):
        for screen in ('REWARDS','MAP_SCREEN'):
            left=fixture(screen); right=copy.deepcopy(left)
            if screen=='REWARDS':
                right['legal_options'][0]['semantics']['card']['id']=99
            else:
                right['legal_options'][0]['semantics']['destination']['room_symbol']='E'
                right['public_state']['map_graph']['nodes'][0]['room_symbol']='E'
                right['public_state']['visible_act_boss']='GUARDIAN'
            a,b=json.dumps(left),json.dumps(right)
            self.assertEqual(treatment(a,'masked'),treatment(b,'masked'))
            self.assertNotEqual(features(treatment(a,'visible'),0),features(treatment(b,'visible'),0))
            self.assertEqual(features(treatment(a,'masked'),0),features(treatment(b,'masked'),0))
            self.assertNotIn('SECRET_OFFER',json.dumps(treatment(a,'masked')))
            self.assertEqual(treatment(a,'visible')['public_state']['persistent_resources'],
                             treatment(a,'masked')['public_state']['persistent_resources'])

    def test_gradients_nonconstant_scores_and_parameter_budget(self):
        value=json.dumps(fixture())
        for arm in ('visible','masked'):
            model=Scorer(430081); before=model.state()
            data=treatment(value,arm)
            rows=[(features(data,i),i/3,1) for i in range(3)]
            self.assertGreater(model.update(rows),0)
            self.assertNotEqual(before,model.state())
            scores=model.scores(value,arm)
            self.assertGreater(max(scores)-min(scores),0)
            self.assertIn(model.choose(value,arm),range(3))
            self.assertEqual(sum(map(len,model.w))+len(model.b)+len(model.v)+1,2081)

    def test_history_does_not_copy_candidates_or_labels(self):
        history=[{'pre_decision':{'screen':'REWARDS','candidate_actions':['SECRET_CARD']},
                  'selected_action':{'index':0,'candidate':{'label':'SECRET_CARD'}},
                  'resource_change':{'hp':-3}}]
        encoded=with_public_history(json.dumps(fixture()),history)
        self.assertNotIn('SECRET_CARD',encoded)
        self.assertEqual(json.loads(encoded)['public_state']['history'][0]['resources'],{'hp':-3})

    def test_general_location_retained_in_both_arms(self):
        context={'current':{'location':{'act':1,'floor':4,'room_type':'MONSTER','map_node':{'room_symbol':'E'}}}}
        encoded=with_public_history(json.dumps(fixture()),[],context)
        for arm in ('visible','masked'):
            state=treatment(encoded,arm)['public_state']
            self.assertEqual(state['ordinary_location'],{'act':1,'floor':4,'room_type':'MONSTER'})
            self.assertNotIn('map_node',state['ordinary_location'])

    def test_graph_relations_not_only_marginals(self):
        left=fixture('MAP_SCREEN');right=copy.deepcopy(left)
        left['public_state']['map_graph']['edges']=[{'from_x':0,'to_x':1},{'from_x':1,'to_x':0}]
        right['public_state']['map_graph']['edges']=[{'from_x':0,'to_x':0},{'from_x':1,'to_x':1}]
        self.assertNotEqual(features(treatment(json.dumps(left),'visible'),0),features(treatment(json.dumps(right),'visible'),0))


if __name__=='__main__':
    unittest.main()
