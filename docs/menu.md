# The menu keyboard and a clean chat

Fork `sawrus/aipager`.

## Every command on the keyboard

In a private chat the main keyboard has a button for every command you use:

| Row | Buttons |
|---|---|
| sessions | one button per live session |
| words | `status`, `stop`, `new` |
| menu | `📚 Resume`, `⏩ Now`, `🔐 Mode` |
| menu | `⚙️ Settings`, `🌐 Language`, `❓ Help` |
| more | `Templates`, `Commands` |

A tap runs the command as if you typed it alone (`/resume`, `/now`, `/mode`,
`/settings`, `/language`, `/help`). The buttons follow the chat's language.
A group keeps its keyboard as it was.

## The `/` command menu is private

aipager registers the `/` command list only for the chats it serves: each
private chat, and in a group each named member (not the group as a whole).
A person in another chat, or a group member who is not on the list, sees
no commands. At every start aipager also deletes the lists in the Default,
all-private-chats, all-group-chats and all-chat-administrators scopes (with
no language and in each language of the project), deletes a group-wide
list that an older version set, and resets the default menu button.

## A tap clears the chat

A tap that opens a view (`status`, `new`, `Resume`, `Mode`, `Settings`,
`Language`, `Help`) first deletes the chat's messages since the previous
such tap, so the chat shows only what the tap brings. The first time it
looks back 2000 messages.

It keeps:

- the message of the tap itself (an empty chat makes Telegram show its
  Start button);
- the cards of sessions at work and the messages of open prompts, which you
  still answer.

`stop` and `Now` act on the work in progress, so they keep the chat as it
is. Telegram deletes only messages younger than 48 hours.

To keep every message, set in `~/.config/aipager/aipager.yaml`:

```yaml
menu:
  clean_chat: false
```
