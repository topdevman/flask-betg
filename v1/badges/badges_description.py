"""
Describe new badges here.
User bounded properties must be in dict inside lambda function to prevent
 unwanted objects mutations.

You can only ADD new user_bounded properties.
All changes in existing user_bounded properties will be ignored
 after first update(insert).

Badge static properties:
    - "type": "one_time", "count"
    - type-specific-for-chosen-type, e.g. "one_time_type", "count_type"
    - if "type"=="count":
        - max_value  # maximum value receive badge
    - name  # user friendly badge name
    - image_path  # path to badge image

Types of badges:
    1) one_time:
        one_time_type:
            - "win"
            - "loss"
            - "draw"
            - "all"
    2) count:
        count_type:
            - "win"
            - "loss"
            - "draw"
            - "all"

User bounded (this part will be stored in DB!) values for type:
    1) one_time:
        - received: bool # required
    2) count:
        - received: bool  # required
        - value: int  # required

All dicts must be flat.

Look below if you understood nothing.
"""

GAMES_GENERAL_ONE_TIME_BADGES = {
    "group_name": "games_general_one_time",
    "badges": {
        "first_win": {
            "type": "one_time",
            "one_time_type": "win",
            "name": "First win!",
            "image_path": "path_to_image",

            "user_bounded": (lambda: {
                "received": False
            })
        },

        "first_loss": {
            "type": "one_time",
            "one_time_type": "loss",
            "name": "First loss!",
            "image_path": "path_to_image",

            "user_bounded": (lambda: {
                "received": False
            })
        }
    }
}

GAMES_GENERAL_COUNT = {
    "group_name": "games_general_count",
    "badges": {
        "played_10_games": {
            "type": "count",
            "count_type": "all",
            "max_value": 10,
            "name": "10 games!",
            "image_path": "path_to_image",

            "user_bounded": (lambda: {
                "value": 0,
                "received": False
            })
        }
    }
}

FIFA15_BADGES = {
    "group_name": "fifa15_xboxone",
    "badges": {
        "fifa15_xboxone_first_win": {
            "type": "one_time",
            "one_time_type": "win",
            "name": "Badge 1",
            "image_path": "path_to_image",

            "user_bounded": (lambda: {
                "received": False
            })
        },

        "fifa15_xboxone_10_wins": {
            "type": "count",
            "count_type": "win",
            "max_value": 10,
            "image_path": "path_to_image",

            "user_bounded": (lambda: {
                "value": 0,
                "received": False
            })
        },

        "fifa15_xboxone_first_loss": {
            "type": "one_time",
            "one_time_type": "loss",
            "name": "Badge 1",
            "image_path": "path_to_image",

            "user_bounded": (lambda: {
                "received": False
            })
        }
    }
}

# contains all badges by groups
BADGES = {
    GAMES_GENERAL_ONE_TIME_BADGES["group_name"]: GAMES_GENERAL_ONE_TIME_BADGES["badges"],
    GAMES_GENERAL_COUNT["group_name"]: GAMES_GENERAL_COUNT["badges"],
    FIFA15_BADGES["group_name"]: FIFA15_BADGES["badges"],
}

