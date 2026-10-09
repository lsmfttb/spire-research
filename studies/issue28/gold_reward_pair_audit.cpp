// Study-only fixture for the Issue #28 native lineage comparison.
// Build against each pinned sts_lightspeed tree's GameAction.cpp and sources.
#include <iostream>
#include <vector>

#include "game/GameContext.h"
#include "sim/search/GameAction.h"

int main() {
    using namespace sts;

    GameContext root;
    root.screenState = ScreenState::REWARDS;
    root.info.rewardsContainer.goldRewardCount = 2;
    root.info.rewardsContainer.gold[0] = 17;
    root.info.rewardsContainer.gold[1] = 41;

    const auto allActions = search::GameAction::getAllActionsInState(root);
    std::vector<search::GameAction> goldActions;
    for (const auto &action : allActions) {
        if (action.getRewardsActionType()
                == search::GameAction::RewardsActionType::GOLD) {
            goldActions.push_back(action);
        }
    }
    if (goldActions.size() != 2) {
        std::cerr << "expected two gold candidates, got " << goldActions.size() << '\n';
        return 2;
    }

    bool mappingsComplete = true;
    for (std::size_t candidate = 0; candidate < goldActions.size(); ++candidate) {
        const auto &action = goldActions[candidate];
        GameContext branch = root;
        const int expected = root.info.rewardsContainer.gold[candidate];
        const bool legal = action.isValidAction(root);
        action.execute(branch);
        const int actual = branch.gold - root.gold;
        const bool mapped = legal && actual == expected;
        mappingsComplete = mappingsComplete && mapped;
        std::cout << "candidate=" << candidate
                  << " action_idx1=" << action.getIdx1()
                  << " legal=" << (legal ? "true" : "false")
                  << " expected_gold=" << expected
                  << " actual_gold=" << actual
                  << " mapping=" << (mapped ? "true" : "false") << '\n';
    }
    std::cout << "candidate_mapping_complete="
              << (mappingsComplete ? "true" : "false") << '\n';
    return 0;
}
