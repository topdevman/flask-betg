from flask import signals

# TODO: clean up mess with namings and app definition
from v1.main import db
from v1.badges import BADGES


badges_namespace = signals.Namespace()
general_one_time_badge_signal = badges_namespace.signal("general_one_time_badge_signal")
game_badge_signal = badges_namespace.signal("game_badge_signal")  # count and one_time


@general_one_time_badge_signal.connect
def update_general_one_time_badge(player, badge_id, **extra):
    """Use this for single non game actions
    if you now exactly badge name for some event"""
    player_badges = player.get_badges(badge_id)
    badge = getattr(player_badges, badge_id)

    if badge["received"]:
        return True

    badge["received"] = True

    player_badges.update_for_notifications(badge_id)

    db.session.flush()

    return True


@game_badge_signal.connect
def update_games_badges(game, **extra):
    """Signal handler for games badges"""
    # FIXME: replace "-" on "_" in gametypes in pollers, required for column naming
    gametype = game.gametype.replace("-", "_")

    creator_badges, badges_ids_with_groups = game.creator.get_badges_from_groups(
        gametype, "games_general_count", "games_general_one_time",
        return_ids=True
    )
    opponent_badges = game.opponent.get_badges_from_groups(
        gametype, "games_general_count", "games_general_one_time"
    )

    def update_badge(user_badge: dict, badge_id: str, group: str, creator: bool):
        if user_badge["received"]:
            return False

        badge_desc = BADGES[group][badge_id]

        if badge_desc["type"] == "one_time":
            one_time_type = badge_desc["one_time_type"]

            if game.winner == "creator":
                if one_time_type == "win" and creator:
                    user_badge["received"] = True

                elif one_time_type == "loss" and not creator:
                    user_badge["received"] = True

            elif game.winner == "opponent":
                if one_time_type == "win" and not creator:
                    user_badge["received"] = True

                elif one_time_type == "loss" and creator:
                    user_badge["received"] = True

            elif game.winner == "draw" and one_time_type == "draw":
                user_badge["received"] = True

            return user_badge["received"]

        elif badge_desc["type"] == "count":
            init_value = user_badge["value"]
            count_type = badge_desc["count_type"]

            if count_type == "all":
                user_badge["value"] += 1

            else:
                if game.winner == "creator":
                    if count_type == "win" and creator:
                        user_badge["value"] += 1

                    elif count_type == "loss" and not creator:
                        user_badge["value"] += 1

                elif game.winner == "opponent":
                    if count_type == "win" and not creator:
                        user_badge["value"] += 1

                    elif count_type == "loss" and creator:
                        user_badge["value"] += 1

                elif game.winner == "draw" and count_type == "draw":
                    user_badge["value"] += 1

            if badge_desc["max_value"] == user_badge["value"]:
                user_badge["received"] = True

            return user_badge["received"] or init_value != user_badge["value"]

    creator_updated_badges = []
    opponent_updated_badges = []

    for badge_id, group in badges_ids_with_groups:
        if update_badge(getattr(creator_badges, badge_id), badge_id, group, True):
            creator_updated_badges.append(badge_id)

        if update_badge(getattr(opponent_badges, badge_id), badge_id, group, False):
            opponent_updated_badges.append(badge_id)

    if creator_updated_badges:
        creator_badges.update_for_notifications(*creator_updated_badges)

    if opponent_updated_badges:
        opponent_badges.update_for_notifications(*opponent_updated_badges)

    db.session.flush()

    return True
