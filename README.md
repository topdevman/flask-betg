Bet game API
============

Authorization
-------------
Most API endpoints require authorization.
(In fact, the only ones not requiring it are user registration and login.)
For authorization you should include an auth token with your request.
That token can be included with any of the following ways:

 * Add an `Authorization` header with value of `Bearer your.token.here`
 * Add a `token` parameter to the request.

Token can be obtained either when registering new user (`POST /players`)
or with dedicated login method (`POST /players/login`).
Token is valid for one year, but can be invalidated by changing password.

Parameters for endpoints can be passed either as GET arguments, as POST form data
or in a JSON object (with corresponding content-type).

Endpoints
---------
For all player-related endpoints which include nickname/id in url,
you can use `_` instead of nickname
and add `id` parameter (either in GET-style or POST-style) containing that value.
This might help with some libraries which fail with urls containing spaces and special characters.

Also you can use `me` alias which means «player currently logged in».

### Workflow guide
Here is a list of endpoints which will be called in typical user's workflow.
It might differ somewhat from what you have in designs,
but should cover all existing endpoints,
so it should be easy to adapt it to design.

* User first installed an app.
  App asks him if he want to register as a new user, login, or register/login with Facebook.
    + If user chooses plain registration, you should ask him to enter
      nickname, email, password
      and optionally EA gamertag.
      Then you call `POST /players` endpoint
      passing the data you got from user
      and this device's push token.
    + If user chose to log in, you will ask to enter name
      (which can be either email, nickname or gamertag) and password.
      Then you call `POST /players/<name entered by user>/login`
      passing password and device's push token.
      **As an alternative**, if your SDK doesn't allow
      whitespaces and special characters in URL,
      you should use `POST /players/_/login` endpoint
      and add `id` parameter with name entered by user.
      This approach works for all endpoints requiring user's id in url.
    + If user chose Facebook login, you should use Facebook api
      to retrieve facebook auth token.
      Please request `email` permission for that token
      because the server will fetch user's email address from Facebook.
      You then call `POST /federated_login` endpoint
      passing the token retrieved from Facebook API.
        - Federated login endpoint returns either `200 OK` or `201 CREATED` code
          and `created` boolean field.
          If it returns `created=true` then you need to ask the user
          to modify email (because facebook may not return user's email),
          modify nickname (because it is set to Facebook user name
          and the user might want to change it),
          and enter EA gamertag (optionally).
          You can prefill data from object returned by `POST /federated_login` endpoint.
* Now that the user logged in, he needs to choose the game to bet on.
  Game types are listed with `GET /gametypes` endpoint with `full=true` option.
  It will return list of endpoints with parameters: for details consult with endpoint description.
  You should only allow the user to choose `supported` gametypes,
  because he will not be able to `POST /games` for unsupported ones.
    * You will need to show images for that gametypes.
      For that you should use `GET /gametypes/<type>/image` endpoint
      which returns `image/png` binary image.
      By default it has maximum size available, but you can ask the system to shrink it
      by passing either `w`, `h` or both parameters.
      If you pass both of them, image will be shrank down and then cropped to fit.
* After the user chose game type, he will want to bet.
  For that you should use `POST /games` endpoint.
  The user chooses his opponent (either by nickname, gamertag or email),
  the opponent should be already registered on our service.
  Both user and opponent should have "identity" field filled -
  the field whose name is denoted in `identity` value of selected gametype.
  *NEW:* Alternatively the user may want to bet for some other players' game result.
  In such case he will provide `gamertag_creator` and `gamertag_opponent`
  to specify IDs of players for which he want to bet;
  in this situation it is not needed to have identity field filled.
  Also the user chooses `gamemode` (from options provided for selected gametype by `GET /gametypes`).
  And the last, the user should enter bet amount, i.e. how many coins will he bet.
  That amount should not exceed user's balance.
  After posting, the game has `new` status.
* If another user invited you to compete, you will receive PUSH notification about that.
  Also you will see new game in `GET /games` endpoint result.
  You can then accept or decline an invitation by calling `PATCH /games/<id>`
  and passing corresponding value in `state` field.
  Note that you cannot accept an invitation if your balance is insufficient,
  so if that endpoint returns `400` error with `problem=coins` parameter
  you should redirect user to balance deposit screen.
* After your invitation is accepted, you will get corresponding PUSH notification
  and game is immediately considered started.
  This is important point because if you are already playing with that same user
  when he accepted an invitation
  then it is result of ongoing game which will be used to determine
  win or loss here - of course only in case gamemode matches.
* When you win or lose and system notices it, you will get corresponding PUSH notification.
  Also, if you win, you will get an email message.
  `balance` will increase, and `locked` part of balance will decrease.
  So `available` balance will increase 2x the bet amount (your money + your win).
  Also result can be a `draw`.
* To deposit funds (i.e. buy internal coins), you should use `POST /balance/deposit` endpoint.
  It accepts `payment_id` returned by PayPal SDK - see link in endpoint description.
  One coin is equivalent to $1 USD.
  If the user pays in another currency, it will be converted to coins
  according to actual exchange rate (returned by [fixer.io](Fixer.io) service).
  If you want to let the user know how much coins will he get,
  call `POST /balance/deposit` endpoint with `dry_run=true` parameter.
  It will return how many coins would user get,
  but will not check transaction and will not change actual balance.
* For payouts use `POST /balance/withdraw` endpoint.
  Like the previous one, it accepts `dry_run` flag which allows to determine
  how much money will the user get for given amount of coins.
   

### POST /players
Player registration.

*Arguments:*

 * `nickname` - required
 * `email` - required
 * `password`
 * `facebook_token` - optional
 * `ea_gamertag` - optional, should match the one used on EA Games
 * `riot_summonerName` - optional, should match the one used on RIOT (League of Legends)
 * `steam_id` - optional, should be STEAM ID of any kind:
	either integer ID (32- or 64-bit), STEAM_0:1:abcdef, or link to SteamCommunity portal
 * `starcraft_uid` - optional, should be a link to user profile either on battle.net or sc2ranks.com
 * `push_token` - device identifier for push notifications - only for login-related methods.
Can be omitted and provided later with `POST /players/<nick>/pushtoken` endpoint.
 * `bio` - optional player's biography (text)
 * `userpic` - this field can be passed as an uploaded file. It has to be a PNG.

 * `_force`: force registration with invalid gamertag (effectively disables gamertag validation). Should not be used for production.

Returns object with Player resource and auth token. Returns `201` HTTP code.
```json
{
	"player": {Player resource},
	"token": "authentication token"
}
```

### GET /players
Retrieve list of players.
This query is paginated.
By default, it returns all players registered, but output can be filtered.

This endpoint returns at most first 20 results.

*Arguments*:

* `filter` - text against which any of player identities should match for that player to be included.
* `filt_op` - operation for filter matching, choices are `startswith` and `contains`, default is `startswith`.
  Matching is always case-insensitive.
* `order` - sorting order, optional. By default players returned are sorted by id, in ascending order.
  Prepend with `-` for descending order.
  Here is a list of supported orders:
    + `lastbet`: order by time of last bet invitation made/received by the player -
      note that it doesn't have to be accepted;
    + `popularity`: order by count of accepted bet invitations (including ones sent by this player);
    + `gamecount`: order by count of games this player has - they include all games: new, accepted, declined and finished
    + `winrate`: order by `winrate` field.
      Note that if you sort by win rate, players will also be sorted by `gamecount` as a secound key.
      This is to ensure list order will be adequate even if some players have no finished games.
  Also player `id` is always used as last key to ensure stable ordering.
  If you choose descending ordering, `id` will also sort descending.
  If you don't specify any ordering, players will be sorted by `id` ascending.
* `gametype` - when ordering by `popularity`, `gamecount` or `winrate`, only consider games with given gametype.
* `period` - when ordering by `popularity`, `gamecount` or `winrate`, only consider games which occured within given period.
  Possible choices: `today`, `yesterday`, `week`, `month`.

Note that when limiting considered games by `gametype` or `period`, system will print out values for non-limited query!
This may be fixed later.

Result:
```json
{
	"players": [
		// list of Player resources
	],
}
```

### GET /players/<id>
Retrieve given player's data.

ID may be either integer internal ID, player's nick or `me`.

For the player requesting will return whole info;
for other players will only return *Limited Player resource*.

You can also set `with_stat` parameter to `true`,
then you will get additional fields `gamecount`, `winrate` and `leaderposition`.

### GET /players/<id>/userpic
Returns given player's userpic with `image/png` MIME type.
If given user has no userpic, will return HTTP code `204 NO CONTENT`.

### GET /players/<id>/recent_opponents
Returns list of recent opponents of current player.
Only can be called for self.

### GET /players/<id>/winratehist
Only can be called for self.
Will return win history data for graph building.

Parameters:

* `interval`: either `day`, `week` or `month`
* `range`: count of `interval`s to be returned

In the output intervals will be placed in reverse time order, i.e. latest first.

`wins` value may be a fraction, because game ended as a draw is considered half-win.

```json
{
	"history": [
		{
			"date": "Tue, 18 Aug 2015 23:04:57 GMT", // start of the interval
			"games": 5,
			"wins": 2.5, // float, 0..games
			"rate": 0.5 // float, 0..1
	]
}
```

### GET /players/<id>/leaderposition
Calculates and returns leaderboard position for given player id.
Position is calculated according to `GET /players?order=-winrate` query.

```json
{
	"position": 9
}
```

### PUT /players/<id>/userpic
This is an alternate way to specify userpic.
Accepts `userpic` parameter containing a file to be uploaded.
File has to be in PNG format.
Upon success, returns `{"success": true}`.

Also available as `POST /players/me/userpic`.

### PATCH /players/<id>
Update player's data.
Accepts any of not-login-related arguments of `POST /players`.
If you provide `password` field, you should also provide `old_password` field
which will be validated against user's current password.
Although it is not required if user has no password configured
(i.e. if he was registered with Facebook).

ID may be either integer internal ID, player's nick or `me`.

Returns Player resource.


### POST /players/<nick>/login
Receive a login token.

In url you can include either `nickname`, `ea_gamertag` (or other identity) or email address.

*Arguments*:

 * `password`
 * `push_token` of the current device.
Can be omitted and provided later with `POST /players/<nick>/pushtoken` endpoint.

### POST /players/<nick>/pushtoken
Set push token if it was not provided during login or registration.

*Arguments*:

 * `push_token` - required.

Returns `{"success": true}` on success.
Will return error if you already specified push token on login/registration.
Also will return error if there is no device id in auth token,
which may happen if token was issued before this endpoint was implemented.

### POST /players/<nick>/logout
Revoke current device's push token.
Parameters:

* `push_token` - optional, you should provide it unless you provided push token during login.
The reason is that if you provided push token initially, the server will assign it with your auth token.
And if you provided push token later using `/players/me/pushtoken` endpoint then server doesn't know which token corresponds to your device.

Returns `{"success": true}` unless error happens.

### POST /federated_login
Federated login via Facebook or Twitter.

*Arguments*:

 * `svc`: service to use: `facebook`, `twitter` or `williamhill`. Defaults to `facebook` for compatibility.
 * `token`: Facebook or Twitter auth token.
	For twitter you should provide both token and secret divided by `:`:
    `...?svc=twitter&token=ACCESS_TOKEN:ACCESS_SECRET` (replace with actual tokens)

For Facebook, token should be requested with `email` permission for server to be able to fetch user's email.

Nickname will be assigned automatically according to Twitter/FB display name,
avoiding any duplicates by adding a number. Later the user may wish to change nickname.

If the user has no userpic provided (wheter it is newly created user or existing one),
this api call will try to fetch userpic from social service.

For WilliamHill, token should be requested by sending user to `https://betgame.co.uk/v1/cas/login' address in a webview.
Then you shall monitor that webview and catch a moment when it will load url starting with `https://betgame.co.uk/v1/cas/result?`.
After that, if no error occured, webview's `title` will contain a JSON object with the following format:
`{"success": true, "token": "TGT-Some-Token"}`. You should take a `token` from that string (starting with `TGT-`)
and pass it to `POST /federated_login` endpoint to continue.
Alternatively you can parse token from url parameters.

This endpoint returns object identical to `POST /players` or `POST /players/<nick>/login`,
depending on whether this player was already registered or not.
HTTP code will be `201` or `200`, accordingly.

### POST /players/<nick>/reset_password
Initiate password recovery.
Will send password changing link to user's registered email.
User can be identified by either gamertag or email address.

*Arguments*: none.

*Result*:
```json
{
	"success": true, // or false if some error occurs
	"message": "Descripting message" // probably error description
}
```

### GET /players/<nick>/messages
Returns list of messages between requesting user and given player.
If `<nick>` is `me`, will return all messages for requesting user.
This query is paginated.

Paramerers:

* `results_per_page` defaults to 10, max is 50
* `page` - which page to return, defaults to 1
* `order` - either `time` or `-time`.
	Default is `time`, while `-time` means descending order.

```json
{
	"messages": [ list of Chat Message resources ]
}
```

### GET /players/<nick>/messages/<id>
Returns single Chat Message resource.

### POST /players/<nick>/messages
Creates new message for player `<nick>`.

Available parameters:

* `text`: message text

Or you can attach a media file using `attachment` parameter (as with userpic).

Returns newly created Chat Message resource on success.

### PATCH /players/<nick>/messages/<id>
This endpoint allows to change message `unread` state, i.e. mark message as read or as unread.

* `viewed`: boolean

Returns Chat Message resource.

### GET /players/<nick>/messages/<id>/attachment
Returns body of message attachment (if any) with proper MIME type.
Will return `204 NO CONTENT` if that message has no attachment.


### GET /balance
Learn current player's balance.

Arguments: none

Result:
```json
{
	"balance": { Balance resource }
}
```


### GET /balance/history
Get transactions history for current player.

This is a paginated query, just like `GET /games`.

Result:
```json
{
	"transactions": [ list of Transaction resources ],
	"page": 1 // current page
	"total_pages": 9,
	"num_results": 83, // total count
}
```

### POST /balance/deposit
Buy internal coins for real money.
You should use [https://github.com/paypal/PayPal-iOS-SDK](PayPal SDK for iOS) or similar.

Arguments:

 * `currency`
 * `total` (value in that currency)
 * `transaction_id` (for real transactions)
 * `dry_run` - set to True and omit `transaction_id` if you want to just determine current exchange rate.

Returns:
```json
{
	"success": true,
	"dry_run": false,
	"added": 25, // in coins
	"balance": { Balance object }
}
```


### POST /balance/withdraw
Sell internal coins for real money.

Arguments:

 * `paypal_email`: email of paypal account which should receive coins
 * `coins`: how many coins do you want to sell
 * `currency`: which currency do you want to get as a result
 * `dry_run`: optional; if set to True, don't actually transfer coins but only return rate etc

Result:
```json
{
	"success": true, // boolean
	"paid": {
		"currency": "USD",
		"value": 10.5,
	},
	"dry_run": false,
	"transaction_id": "Transaction Identifier",
	"balance": { Balance resource }
}
```


### GET /gametypes
List available game types.

*Arguments:*

* `betcount`: whether to include count of bets (i.e. popularity)
  and last bet time for each gametype; defaults to `false`
* `latest`: whether to include `latest` list; defaults to `false`
* `identities`: whether to include list of all available identities. Defaults to `true` *but is forced to `false` if `filter` is provided*.
* `filter`: text to search in `name` or `subtitle` of games. Will not apply filtering by default. Search is case-insensitive.
* `filt_op`: filtering operation, either `startswith`, `contains` or `endswith`. Defaults to `startswith`.

Will return the following detials about each game type:

* `id` - internal identifier used for that gametype
* `name` - human-readable name of gametype
* `subtitle` - subtitle for the game, may be `null`
* `category` - human-readable category name
* `description` - textual description of how to bet and play that specific game.
  Might be `null` if not provided / not required.
  This field may consist of multiple paragraphs divided by `\n` endline character.
* `supported` field - if it is `false` then the only thing you can do with this gametype
is to fetch its image with `GET /gametypes/<type>/image`;
* `gamemodes` lists possible gamemodes for this game type;
* `gamemode_names` lists suggested user-visible names for each gametype;
* `identity` tells which field in player info is used to identify player for this game type.
  When you call `POST /games`, you can provide that IDs as `gamertag_*` values,
  or they will be read from user's profile.
  For example, for FIFA games identity is `ea_gamertag`.
  This means that if you don't provide `gamertag_creator` field,
  system will look for gamertag in your `ea_gamertag` profile field.
  For other game types special fields will be added in future.
* `identity_name` - human-readable description of identity
* `twitch` - whether twitch link is supported for this gametype:
  `0` means unsupported,
  `1` means optional (i.e. game results can be fetched with other means, but slower),
  and `2` means mandatory (i.e. twitch is the only result polling method for this game).
* `twitch_identity` - identity ID for twitch, in case separate identity is required for it, or `null`
* `twitch_identity_name` - human-readable description of `twitch_identity` (if any) or `null`
* `betcount` (if requested) - how many bets were made on this gametype
* `lastbet` (if requested) - when latest bet was made on this gametype, or `null` if no bets were made

Also, for convenience, it returns separate `identities` list
which contains all possible identity fields stored in `Player` resource.
That list may change when we add support for new games,
so it is advised to fetch it from the server rather than hardcode.

And if `latest` parameter is set to `true`,
this endpoint will also return `latest` list ordered by data descendingly
showing last betted gametypes.

```json
{
	"gametypes": [
		{
			"id": "fifa14-xboxone",
			"name": "FIFA-15",
			"supported": true,
			"gamemodes": {
				"fifaSeasons": "FIFA Seasons,
				"fut": "FUT",
				"friendlies": "Friendlies",
				...
			],
			"identity": "ea_gamertag",
			"identity_name": "EA Games GamerTag",
			"twitch": 1,
			"twitch_identity": "fifa_team",
			"twitch_identity_name": "FIFA Team Name"
		},
		...,
		{
			"id": "destiny",
			"name": "Destiny",
			"supported": false
		},
		...
	},
	"identities": {
		"ea_gamertag": "EA Gamertag",
		"riot_summonerName": "RIOT Summoner Name",
		...
	},
	"latest": [
		{
			"gametype": "league-of-legends",
			"date": "datetime_object"
		},
		...
	]
}
```

### GET /gametypes/<type>/image
Retrieves a cover image for given game type.

Arguments:

 * `w`: image width (defaults to maximum possible)
 * `h`: image width (defaults to maximum possible)

If only one of arguments is provided, other will be chosen to maintain aspect ratio.
If both are provided, image will be cut to keep aspect ratio.

Returns image itself with corresponding MIME type (most likely PNG).

If image not found for requested gametype, 404 error will be returned.


### GET /gametypes/<type>/background
Retrieves background picture for given game type.
Arguments and behaviour is the same as for `GET /gametypes/<type>/image` endpoint.
Background images are generally different from cover images, have better resolution, 
and exist only for supported games.

### GET /identities
Returns list of all supported identities, like `identities` field of `GET /gametypes` but with more details.
For some identities it may include `choices` object which maps possible identity values to human-readable values.
Note that you should allow user to enter not-listed short values as well.

```json
{
	"identities": [
		{
			"id": "fifa_team",
			"name": "FIFA Team Name",
			"choices": {
				"Manchester United": "MUN",
				"Monaco": "MON",
				...
			}
		},
		{
			"id": "ea_gamertag",
			"name": "XBox GamerTag",
			"choices": null
		},
		...
	]
}
```



### POST /games
Create game invitation.

You must specify `bet` and `opponent_id` (and not `tournament_id`) for simple game
You must specify `tournament_id` (and not `bet` and `opponent_id`) for tournament game

Arguments:

* `opponent_id`: either nickname, gamertag or internal numeric id of opponent.
* `root_id`: id of `game` object which should be "root" for this one,
  i.e. which denotes current gaming session.
  This argument can be omitted if you are creating new gaming session.
* `gamertag_creator`: gamertag of the player for which invitation creator roots.
	Optional, defaults to creator's own gamertag (if specified).
* `gamertag_opponent`: gamertag of the player for which invitation opponent roots.
	Optional, defaults to opponent's own gamertag (if specified).
* `savetag`: optional. Controls updating creator's default identity for given gametype. Here are options:
    * `never` (default) - don't update
    * `replace` - always replace player's identity with passed one
    * `ignore_if_exists` - if player has no corresponding identity then save,
       else ignore.
    * `fail_if_exists` - if player has no corresponding identity then save,
       else abort query (without creating game object).
       You can then ask user what to do and then resend query with either `never` or `replace`.
* `gametype`: one of `supported` gametypes from `GET /gametypes` endpoint
* `gamemode`: one of game modes allowed for chosen gametype according to `GET /gametypes`.
* `bet`: numeric bet amount, should not exceed your balance.
* `twitch_handle`: either full URL to Twitch stream or its last part (handle).
	Optional unless gametype requires it.
* `twitch_identity_creator`: player identity (like gamertag) for twitch stream.
	Prohibited if given gametype doesn't support it.
	If not passed, defaults to creator's corresponding gamertag (if specified).
* `twitch_identity_opponent`: player identity (like gamertag) for twitch stream.
	Prohibited if given gametype doesn't support it.
	If not passed, defaults to opponent's corresponding gamertag (if specified).
* `tournament_id` for tournament games

When creating an invitation, corresponding amount of coins is immediately locked on user's account.
These coins will be released when invitation is declined
or when the game finishes with either win or draw of creator.

Returns *Game resource* on success.

### GET /games
Retrieve games (both accepted and not accepted yet) available for current player -
i.e. either initiated by or sent to them.

This request supports pagination:

 * `page`: page to return (defaults to 1)
 * `results_per_page`: how many games to include per page (defaults to 10, max is 50)

Also results can be sorted:

 * `order`: ordering way. Sorts ascending by default; prepend with '-' to sort descending.
   Allowed fields for sorting: `create_date`, `accept_date`, `gametype`, `creator_id`, `opponent_id`.

Return:
```json
{
	"games": [
		list of Game resource objects
	],
	"page": 1 // current page
	"total_pages": 9,
	"num_results": 83, // total count
}
```


### GET /games/<id>
Returns details on particular game based on its ID.
Will not return data on games not related to current user.

Return: Game resource


### PATCH /games/<id>
Accept or decline an invitaton.

Arguments possible:

 * `state`: either `accepted` or `declined` for game opponent,
	or `cancelled` for game invitation creator.

Accepting game will immediately lock corresponding amount on player's balance
and the game will be considered started.

If trying to accept and there is no coins enough to cover game's bet amount,
this request will fail with `400` code and additional `problem` field with value `coins`.
In such situation the user should be advised to buy more coins.

Game invitation creator can only make invitation `cancelled`.

Returns *Game resource* object on success.


### DELETE /games/<id>
Request or confirm challenge aborting.

If game's `aborter` field is null or equals to you,
this will initiate an aborting request and return `{"started":true}`.
So the user can request game aborting, and then request it again - new event will be created then.

If game's `aborter` field equals to your opponent
(i.e. your opponent request game aborting),
this call will abort the game and return `{"aborted":true}`.

### GET /games/<id>/msg
Returns binary message file attached to this game, or `204 NO CONTENT` if file was not attached.
Content-type will be passed automatically based on file extension.

### PUT /games/<id>/msg
Attach message to given game, just like `PUT /players/<id>/userpic`.
Message file should be passed as `msg` parameter.
Upon success will return `{"success": true}`.
You cannot upload/change message if game state is not `new`.

For now accepted extensions are `OGG`, `MP3`, `MPG`, `OGV`, `MP4` and `M4A`. I can add more if you need.
Maximum file size is currently 32MB.

This endpoint is also available as `POST /games/<id>/msg` for compatibility.


### GET /games/<id>/messages
Counterpart of `GET /players/<id>/messages` for per-bet messages

### GET /games/<id>/messages/<id>
Get single message for given game

### POST /games/<id>/messages
Send new per-bet message, see `POST /players/<id>/messages` for details.
Message receiver will be your opponent for this game.

### PATCH /games/<id>/messages/<id>
Mark chat message as read.

### POST /games/<id>/report
Report game result
*Arguments*:

 * `result`: won', 'lost' or 'draw'
Returns Report resource

### GET /games/<id>/report
Returns Report resource if you have already reported this game.

### PATCH /games/<id>/report
Change your previous report if you have already reported this game.
*Arguments*:

 * `result`: won', 'lost' or 'draw'
Returns Report resource.


### GET /games/<id>/events
Retrieve events for given *gaming session*.
Game denoted by an ID passed should be the `root` one.
All messages, game state changes and others are represented as events.

Events are sorted by time ascending. I'll add other sorting options and pagination later.

Return format:
```json
{
	"events": [ list of Event resources ]
}
```

### GET /games/<game-id>/events/<event-id>
Retrieves single Event resource.


### GET /games/<game-id>/tickets'
Returns all tickets related to current game.

### GET /tournaments
This request supports pagination:

 * `page`: page to return (defaults to 1)
 * `results_per_page`: how many games to include per page (defaults to 10, max is 50)
* `gametype`: one of `supported` gametypes from `GET /gametypes` endpoint
* `gamemode`: one of game modes allowed for chosen gametype according to `GET /gametypes`.

### GET /tournaments/<id>
Get tournament by id (see tournament resource)

### PATCH /tournaments/<id>
Participate in tournament

### POST /tournaments
Create tournament
 * `rounds_count`
 * `open_date` unixtime timestamp
 * `start_date` unixtime timestamp
 * `finish_date` unixtime timestamp
 * `buy_in` buy in (float)
 * `gametype`: one of `supported` gametypes from `GET /gametypes` endpoint
 * `gamemode`: one of game modes allowed for chosen gametype according to `GET /gametypes`.

### GET /tickets/<ticket_id>/messages
Returns list of messages for ticket `<ticket_id>`.

Paramerers:

* `results_per_page` defaults to 10, max is 50
* `page` - which page to return, defaults to 1
* `order` - either `time` or `-time`.
	Default is `time`, while `-time` means descending order.

```json
{
	"messages": [ list of Chat Message resources ]
}
```

### GET /tickets/<ticket_id>/messages/<id>
Returns single Chat Message resource.

### POST /tickets/<ticket_id>/messages
Creates new message for ticket `<ticket_id>`.

Available parameters:

* `text`: message text

Or you can attach a media file using `attachment` parameter (as with userpic).

Returns newly created Chat Message resource on success.

### PATCH /tickets/<ticket_id>/messages/<id>
This endpoint allows to change message `unread` state, i.e. mark message as read or as unread.

* `viewed`: boolean

Returns Chat Message resource.

### GET /tickets/<ticket_id>/messages/<id>/attachment
Returns body of message attachment (if any) with proper MIME type.
Will return `204 NO CONTENT` if that message has no attachment.

## Debugging endpoints (some of them)

### POST /debug/push_event/<root>/<etype>
Simulates and pushes event with given root challenge id and of given event type.

Possible event types are documented in `Event resource` description.

Also you can provide 4 optional fields:

* `message` - message resource id,
* `game` - game resource id,
* `text` - string,
* `newstate` - string.

If not provided, these fields will be null.

This endpoint returns `{"success": true}` on success.

## PUSH notifications
Whenever an event happens in the system, it will send PUSH notification to related devices.
For now there are 2 types of notifications: event-related and chat-related.

Vast majority of notifications are now in Event-related format. They look like this:
```json
{
	"alert": text depending on event, e.g. "New message from {sender}: {text}" or "Game event detected: {event}",
	"badge": "increment",
	"content_available": 1,
	"event": { Event resource }
}
```

For global chat messages (i.e. which are not linked to any game session)
server will send push notifications in another format:

```json
{
	"alert": "Message from <sendername>: <msg text>",
	"badge": "increment",
	"content_available": 1,
	"message": { Chat Message resource }
}
```

## Socket.io notifications
For web frontend you can use Socket.io protocol for notifications instead of PUSH.
To do this, you should establish a SocketIO connection with `path` set to `/v1/socket.io`.
When connection is established, you should authorize by sending message of type `auth` with token as a payload.
If authorization failed, connection will be dropped;
if auth succeeded, you will start receiving events as messages with type `message`.
Event payload syntax is the same as for PUSH notifications, and can be debugged in the same way.

Here is an example of how this can be implemented:

```js
var socket = io.connect('http://betgame.co.uk', {path: '/v1/socket.io'}); // path is important!
socket.on('connect', function() {
	socket.emit('auth', My_Auth_Token);
});
socket.on('message', function(data) {
	console.log('New event received', data);
});
```

P.S. You may get some `400 Bad Request` errors when establishing connection to socket.
I couldn't fix it for now, but eventually connection is established.

Resources
---------

### Player resource
```json
{
	"id": 23, // internal identifier
	"nickname": "John Smith",
	"email": "user@name.org",
	"facebook_connected": true, // boolean
	"bio": "player's biography, if specified",
	"has_userpic": false,
	"ea_gamertag": "DERP HACKER",
	"fifa_team": "ABT",
	"riot_summonerName": null,
	"steam_id": null,
	"starcraft_uid": null,
	"tibia_character": null,
	"devices": [ list of Device resources ],
	"balance": 3.95, // current balance in coins
	"balance_info": { Balance resource },
	// next fields are included only for `GET /players` endpoint
	"gamecount": 3, // how many game invitations are there with this player, including declined and ongoing ones
	"winrate": 0.4, // 0..1 - percentage of games won; can be `null` if there are no finished games!
	// next field is only included for `GET /players` endpoint ordered by `winrate` or `-winrate`
	"leaderposition": 7, // same as `leaderposition` endpoint
}
```

### Balance resource
```json
{
	"full": 135.2, // how many coins are there
	"locked": 10, // locked coins are ones placed on the table for some active games
	"available": 125.2, // how many coins can you freely use or withdraw - this is full minus locked
}
```

### Transaction resource
```json
{
	"id": 123, // internal identifier, e.g. for tech support usage
	"date": "some datetime", // when this transaction happened
	"type": "withdraw", // one of "deposit", "withdraw", "won", "lost", "other"
	"sum": -100, // amount in coins (either positive or negative, depending on type)
	"balance": 90, // resulting balance in coins *after* this transaction
	"game_id": 135, // for win or lost only - related game id
	"comment": "Converted to 100 USD" // for deposit/withdraw operations
}
```
Comment: `other` transaction type may happen when transaction was made for technical reasons.
One of examples is when user initiates a payout which fails for some reason.
In such situation there will be one transaction of `withdraw` type and another
(with same amount but positive) of `other` type with corresponding description.

### Limited Player resource
Returned if you want to get info about other players.
Doesn't include sensitive information like `balance` or `devices`.
```json
{
	"id": 23, // internal identifier
	"nickname": "John Smith",
	"email": "user@name.org",
	"facebook_connected": true, // boolean
	"bio": "player's biography, if specified",
	"has_userpic": false,
	
	"ea_gamertag": "DERP HACKER",
	"fifa_team": "ABT",
	"riot_summonerName": null,
	"steam_id": null,
	"starcraft_uid": null,
	"tibia_character": null,
}
```

### Device resource
```json
{
	"id": 10, // internal id
	"last_login": "some date"
}
```

### Game resource
Possible game states:

 * `new`: this game is in invitation phase
 * `cancelled`: creator decided to cancel this invitation,
	and it should not be displayed in interface.
 * `declined`: opponent declined an offer
 * `accepted`: opponent accepted an offer and game is considered ongoing, system polls EA servers for result
 * `finished`: system got game outcome from EA servers and already moved bets accordingly

```json
{
	"id": 15, // internal id
	"creator": { Limited Player resource },
	"opponent": { Limited Player resource },
	"is_root": false, // whether this game object is root of game session
	"parent_id": 3, // id of parent (root) game object, or `null` if this is root
	"children": [ list of Game resource without `children` field for bets within this gaming session ]
	"gamertag_creator": "Creator's primary identity",
	"gamertag_opponent": "Opponent's primary identity",
	"identity_id": "ea_gamertag", // value of corresponding gametype's identity_id
	"identity_name": "EA GamerTag", // value of corresponding gametype's identity_name
	"twitch": 1, // see POST /game for options
	"twitch_identity_creator": "Creator's Identity for Twitch Streamer",
	"twitch_identity_opponent": "Opponent's Identity for Twitch Streamer",
	"twitch_identity_id": "Fifa Team", // value of corresponding gametype's twitch_identity_id
	"twitch_identity_name": "Fifa Team", // value of corresponding gametype's twitch_identity_name
	"gametype": "xboxone-fifa15", // see POST /games for options
	"gamemode": "friendlies", // or any other, see POST /games for details
	"is_ingame": false, // whether this game's gamemode is ingame bet
	"bet": 5.29, // bet amount
	"has_message": true, // bool, tells if GET /games/<id>/msg will work
	"create_date": "RFC datetime",
	"state": "finished", // see above
	"accept_date": "RFC datetime", // date of eiter accepting or declining game, null for new games
	"aborter": null or { Limited Player resource },
	"winner": "opponent", // either "creator", "opponent" or "draw"
	"details": "Manchester vs Barcelona, score 1-3", // game result details, it depends on game and poller
	"finish_date": "RFC datetime" // date when this game was finished, according to EA servers
}
```



### Chat message resource
```json
{
	"id": 123,
	"sender": { Limited Player resource },
	"receiver": { Limited Player resource },
	"text": "Message text", // might be `null` if `has_attachment` is `true`
	"time": "RFC datetime",
	"has_attachment": false,
	"viewed": false, // `false` means "unread" message state
}
```


### Event resource
Event types:

* `message`: new chat message was received within this game
* `system`: system event was received about game state
* `betstate`: game state was updated
* `abort`: one of users requested to abort the game

When this resource is sent over PUSH, it will also include `root` field with complete info about root game.

```json
{
	"id": 123,
	"root_id": 10, // id of root game for this event
	"time": "RFC datetime",
	"type": see above,
	"message": Chat Message object (if type = message, else `null`),
	"game": Game resource in which event happened, or `null` for messages,
	"text": "system message text, if any, or null",
	"newstate": "finished", // new game state, for `betstate` type only
}
```

### Tournament resource

```json
{
    "finish_date": "Tue, 05 Jan 2016 07:13:51 -0000", 
    "id": 1, 
    "open_date": "Tue, 05 Jan 2016 02:19:40 -0000", 
    "participants_by_round": [ // toutnament table for bracket tournaments
        [
            [
                { Participant resource }, 
                {
                    "defeated": null,
                    "player": null, // empty participant
                    "round": 0
                }
            ]
        ]
    ], 
    "participants_cap": 2, //max participants
    "rounds_dates": [
        {
            "end": "Tue, 05 Jan 2016 07:13:51 -0000", 
            "start": "Tue, 05 Jan 2016 07:12:11 -0000"
        }
    ], 
    "start_date": "Tue, 05 Jan 2016 07:12:11 -0000",
	"gametype": "xboxone-fifa15", // see POST /games for options
	"gamemode": "friendlies", // or any other, see POST /games for details
}
```

### Participant resource
```json
{
    "defeated": false, 
    "player": {Player resource}, 
    "round": 1
}
```

### Report resource
```json
{
        'result': ,
        'created': "Tue, 05 Jan 2016 07:12:11 -0000",
        'modified': "Tue, 05 Jan 2016 07:13:51 -0000",
        'match': true, // does report match with opponents report?
        'ticket_id': null, // if not ticket created
    }
```

### Ticket resource
```json
{
        'result': ,
        'created': "Tue, 05 Jan 2016 07:12:11 -0000",
        'modified': "Tue, 05 Jan 2016 07:13:51 -0000",
        'match': true, // does report match with opponents report?
        'ticket_id': null, // if not ticket created
    }
```
# flask-betg
