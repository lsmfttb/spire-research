"""Pinned native synthetic reward transitions; zero game/source owner generation.

Run under WSL Python 3.14. This compiles a small harness including pinned native
bindings, links read-only pinned-build objects, and uses DEFAULT GameContext{}.
There is no seeded game constructor, gameplay rollout, battle, or Q generation.
"""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import resource
import subprocess
import tarfile
import time

PIN = '18bcd565b587186425bb46ad3d70a2eb80d947fc'
EXT_SHA = '0652b1ce6d757404278b840678b1f8548c81fb6b03cade598e7f3d82524b18d2'
BUILD = Path('/home/lsmft/.cache/issue45-review-build-18bcd565')
VENDOR = Path('/home/lsmft/stsrl-spikes/sts_lightspeed-t106-5afae22')

HARNESS = r'''
#include "bindings/slaythespire.cpp"
#include <pybind11/embed.h>
#include <iostream>
using namespace sts;
void require(bool ok, const char* msg) { if (!ok) throw std::runtime_error(msg); }
std::vector<LightSpeedAction> fixtureActions(const GameContext &gc) {
    // Exact noncombat enumeration path from StepSimulator::legalActions.
    std::vector<LightSpeedAction> out;
    for (const auto &a : search::GameAction::getAllActionsInState(gc)) out.push_back(makeGameAction(gc,a));
    for (int idx=0;idx<gc.potionCapacity;++idx) {
        const search::GameAction use(0x80000000U | static_cast<std::uint32_t>(idx));
        if(use.isValidAction(gc)) out.push_back(makeGameAction(gc,use));
        const search::GameAction discard(0xC0000000U | static_cast<std::uint32_t>(idx));
        if(discard.isValidAction(gc)) out.push_back(makeGameAction(gc,discard));
    }
    return out;
}
pybind11::dict snapshot(const GameContext &gc) {
    auto acts=fixtureActions(gc); pybind11::dict d; pybind11::list identities;
    for(auto &a:acts) identities.append(publicChoiceActionSnapshot(a));
    d["payload"]=publicRewardScreenPayload(gc,acts); d["candidate_actions"]=identities;
    d["deck"]=deckSnapshot(gc); d["potions"]=gamePotionListSnapshot(gc);
    d["current_hp"]=gc.curHp; d["max_hp"]=gc.maxHp; d["gold"]=gc.gold;
    d["potion_count"]=gc.potionCount; d["potion_capacity"]=gc.potionCapacity;
    return d;
}
search::GameAction find(const GameContext &gc,const std::string &kind,int idx1=0,int idx2=0) {
    for(auto &a:fixtureActions(gc)) if(a.kind==kind && a.idx1==idx1 && a.idx2==idx2) {
        const search::GameAction native(a.bits); require(native.isValidAction(gc),"enumerated action not legal");return native;
    }
    throw std::runtime_error("missing fixture action");
}
int main(){
    pybind11::scoped_interpreter python{};
    GameContext gc{}; // no seed constructor / map or monster generation
    gc.cc=CharacterClass::IRONCLAD;gc.ascension=20;gc.act=1;
    gc.outcome=GameOutcome::UNDECIDED;gc.screenState=ScreenState::REWARDS;
    gc.curHp=40;gc.maxHp=80;gc.gold=99;gc.potionCapacity=2;gc.potionCount=2;
    gc.potions.fill(Potion::EMPTY_POTION_SLOT);gc.potions[0]=Potion::BLOOD_POTION;gc.potions[1]=Potion::FIRE_POTION;
    CardReward first;first.push_back(Card(CardId::ANGER));first.push_back(Card(CardId::BASH,1));
    CardReward second;second.push_back(Card(CardId::CLEAVE));second.push_back(Card(CardId::SHRUG_IT_OFF));
    gc.info.rewardsContainer.addCardReward(first);gc.info.rewardsContainer.addCardReward(second);
    gc.info.rewardsContainer.addPotion(Potion::STRENGTH_POTION);
    pybind11::dict result;result["fixture_kind"]="synthetic_native_default_context_no_game_generation";
    result["before"]=snapshot(gc);
    // Full inventory take is LEGAL but loses the offer without acquisition.
    auto lost=gc;find(lost,"reward_potion").execute(lost);
    require(lost.potionCount==2 && lost.potions[0]==Potion::BLOOD_POTION && lost.potions[1]==Potion::FIRE_POTION,"full take changed inventory");
    require(lost.info.rewardsContainer.potionCount==0,"full take did not remove offer");
    result["take_at_full_inventory"]=snapshot(lost);
    // Discard only changes inventory; every unresolved card group and offer persists.
    find(gc,"game_potion_discard").execute(gc);
    require(gc.potionCount==1 && gc.info.rewardsContainer.cardRewardCount==2 && gc.info.rewardsContainer.potionCount==1,"discard lost unresolved offers");
    result["after_discard"]=snapshot(gc);
    // Taking a card consumes only its group, with surviving group remapped to idx1=0.
    find(gc,"reward_card",0,0).execute(gc);
    require(gc.info.rewardsContainer.cardRewardCount==1 && gc.info.rewardsContainer.cardRewards[0][0].getId()==CardId::CLEAVE,"remaining group changed");
    result["after_card_group0"]=snapshot(gc);
    find(gc,"reward_potion").execute(gc);
    require(gc.potionCount==2 && gc.potions[0]==Potion::STRENGTH_POTION && gc.info.rewardsContainer.potionCount==0,"take after discard failed");
    result["after_potion_take"]=snapshot(gc);
    // Noncombat use is legal for Blood Potion and changes HP/slot; other groups persist.
    auto used=lost;find(used,"game_potion_use").execute(used);
    require(used.potionCount==1 && used.curHp>40 && used.info.rewardsContainer.cardRewardCount==2,"potion use transition failed");
    result["after_noncombat_use"]=snapshot(used);
    // Reward misc is absent in native public reward snapshot, even if underlying card differs.
    auto altered=gc;altered.info.rewardsContainer.cardRewards[0][0].misc=99;
    require(publicRewardScreenPayload(gc,fixtureActions(gc)).equal(publicRewardScreenPayload(altered,fixtureActions(altered))),"reward misc unexpectedly exposed");
    result["reward_misc_not_exposed"]=true;
    result["no_games_generated"]=true;
    std::cout<<pybind11::module_::import("json").attr("dumps")(result,pybind11::arg("sort_keys")=true).cast<std::string>()<<"\n";
}
'''

def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def usage():
    child=resource.getrusage(resource.RUSAGE_CHILDREN)
    return time.process_time()+child.ru_utime+child.ru_stime

def main():
    p=argparse.ArgumentParser();p.add_argument('--output',required=True);p.add_argument('--native-repo',default='D:/DeadlycatCoding/sts_lightspeed');args=p.parse_args()
    output=Path(args.output);output.mkdir(parents=True,exist_ok=True);start=usage()
    receipt={'native_pin':PIN,'fixture_kind':'synthetic default GameContext; no generated owner/run/continuation','status':'starting','stages':[]}
    def stage(name):
        receipt['stages'].append({'stage':name,'cpu_seconds':usage()-start});(output/'visibility-cpu.json').write_text(json.dumps(receipt,indent=2))
    stage('extracting_pinned_source')
    assert sha(BUILD/'slaythespire.cpython-314-x86_64-linux-gnu.so')==EXT_SHA
    raw=subprocess.check_output(['git.exe','-C',args.native_repo,'archive',PIN])
    native=output/'visibility-build'/'native';native.mkdir(parents=True,exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(raw)) as archive: archive.extractall(native,filter='data')
    receipt['source_archive_sha256']=hashlib.sha256(raw).hexdigest()
    source=native/'bindings/slaythespire.cpp';receipt['bindings_sha256']=sha(source)
    audited=['bindings/slaythespire.cpp','src/sim/search/GameAction.cpp','src/game/GameContext.cpp','src/game/CombatReward.cpp','src/game/Deck.cpp','include/game/GameContext.h','include/game/Rewards.h','include/game/Card.h','include/sim/search/GameAction.h','CMakeLists.txt','apps/test-potion-discard-identity.cpp','apps/test_public_noncombat_projection.cpp']
    receipt['audited_pinned_source_sha256']={rel:sha(native/rel) for rel in audited}
    cpp=output/'visibility-build'/'fixture.cpp';cpp.write_text(HARNESS)
    objects=sorted((BUILD/'CMakeFiles/test-potion-discard-identity.dir/src').rglob('*.o'))
    objects += [BUILD/'CMakeFiles/test-potion-discard-identity.dir/bindings/bindings-util.cpp.o']
    assert len(objects)>25
    # Recompile the exact transition/container/deck implementations. Remaining
    # support objects are read-only from the reviewed pinned extension build.
    key_sources=['src/sim/search/GameAction.cpp','src/game/GameContext.cpp','src/game/CombatReward.cpp','src/game/Deck.cpp']
    fresh={}
    for rel in key_sources:
        obj=output/'visibility-build'/ (Path(rel).stem+'.fresh.o')
        subprocess.run(['c++','-std=c++17','-O0','-include','algorithm','-I'+str(native/'include'),'-I'+str(VENDOR/'json/single_include'),'-c',str(native/rel),'-o',str(obj)],check=True,capture_output=True)
        fresh[rel]=obj
        objects=[obj if str(x).endswith(rel+'.o') else x for x in objects]
    receipt['fresh_transition_objects']=[{'source':rel,'source_sha256':sha(native/rel),'object_sha256':sha(obj)} for rel,obj in fresh.items()]
    receipt['linked_objects']=[{'path':str(x),'sha256':sha(x)} for x in objects]
    receipt['vendor_headers']={str(x.relative_to(VENDOR)):sha(x) for base in ['pybind11/include','json/single_include'] for x in (VENDOR/base).rglob('*') if x.is_file()}
    exe=output/'visibility-build'/'fixture'
    cmd=['c++','-std=c++17','-O0','-include','algorithm','-Wno-shift-count-overflow','-I'+str(native),'-I'+str(native/'include'),'-I'+str(native/'bindings'),'-I'+str(VENDOR/'pybind11/include'),'-I'+str(VENDOR/'json/single_include'),'-I/usr/include/python3.14',str(cpp),*map(str,objects),'-lpython3.14','-o',str(exe)]
    receipt['compile_command']=cmd;stage('compiling')
    subprocess.run(cmd,check=True,stdout=(output/'visibility-compile.stdout').open('w'),stderr=(output/'visibility-compile.stderr').open('w'))
    stage('running_native_transitions')
    run=subprocess.run([str(exe)],check=True,capture_output=True,text=True)
    data=json.loads(run.stdout);(output/'visibility-native.json').write_text(json.dumps(data,sort_keys=True,indent=2))
    receipt['output_sha256']=sha(output/'visibility-native.json');receipt['executable_sha256']=sha(exe);receipt['status']='passed'
    stage('complete');print(json.dumps({'passed':True,'cpu_seconds':usage()-start,'output_sha256':receipt['output_sha256']}))

if __name__=='__main__':main()
