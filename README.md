  # 🛡️ Moderator Bot

  **Moderator Bot** is an advanced Discord moderation tool built to maintain a safe, respectful, and well-organized server environment. It offers comprehensive moderation capabilities, including disciplinary enforcement, AI-powered content monitoring, and robust activity tracking.

## ➕ Add the Bot

You can add **Moderator Bot** to your server using the following platforms:

- [Top.gg](https://top.gg/bot/1342035474201575424)  
- [Discord Bot List](https://discordbotlist.com/bots/moderator-bot-9179)

  ## 🔑 Key Features

  ### ⚠️ Strike System (Customizable)
  Implements a structured disciplinary framework to ensure consistent enforcement of server rules, below is the default configuration but it can be changed using  `/settings strike <number_of_strikes> <action> <duration>` :

  - **1st Strike**: 24-hour timeout  
  - **2nd Strike**: 7-day timeout  
  - **3rd Strike**: Permanent ban  

  ### 🤖 AI-Powered Content Moderation
  - **NSFW Content Detection**: Leverages **OpenAI's Moderation API** to identify and log inappropriate content.
  - **Offensive Language Filtering**: Uses AI to detect toxic or offensive messages, ensuring a respectful community.
  - **Context-Aware Filtering**: Analyzes conversation context to reduce false positives and improve detection accuracy.

  ### 📝 Activity Logging
  Provides detailed logs of:
  - Message edits & deletions  
  - Member join/leave events  
  - Timeout & ban actions  
  - Channel/topic changes

  ### 🚫 Banned Words Management
  - Add, remove, and list banned words to automatically filter harmful language.
  - Logs infractions triggered by banned words.

  ### 🔒 Moderator Tools
  - **Customizable Settings**: Easily change or remove moderation configurations.

  ## 📜 Command List

  ### ⚙️ Settings & Configuration
  - `/settings get <name>` – Get the current value of a server setting  
  - `/settings set <name> <value>` – Set a server setting  
  - `/settings remove <name>` – Remove a server setting  
  - `/settings channel_remove <name> <channel>` – Remove a channel from a setting  
  - `/settings channel_set <name> <channel>` – Set a channel for a setting  
  - `/settings strike <number_of_strikes> <action> <duration>` – Configure strike actions  

  ### 🚫 Banned Words
  - `/bannedwords add <word>` – Add a word to the banned list  
  - `/bannedwords remove <word>` – Remove a word from the banned list  
  - `/bannedwords list` – Show all banned words  

  ### ⚖️ Strikes
  - `/strike <user>` – Strike a user  
  - `/strikes get <user>` – Get strikes of a user  

  ### ❓ Help
  - `/help` – Display help info and available settings/commands
