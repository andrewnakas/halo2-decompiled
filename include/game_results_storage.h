/* GAME_RESULTS_STORAGE.H: the game's results, 0xdcd8 bytes at 0x510cb0.
   0x195a40 clears the whole object with one store and fills it from the game
   options when a game starts, so the pieces other sources name (the flags,
   the players' entries, the teams' entries, the counters, the samples and the
   machine addresses) are fields of one object here. Included by globals.h
   after the input types it needs. */

#ifndef GAME_RESULTS_STORAGE_H
#define GAME_RESULTS_STORAGE_H

#include <stddef.h>

struct s_input_entry
{
	byte active;
	byte unknown01;
	short value;
	byte unknown04[0x14];
};

/* eleven bytes: a six byte address, a used flag and four more */
struct s_input_address
{
	byte data[11];
};

/* one recorded sample (unknown_196d20.cpp): its two player indices */
struct s_196d20_sample
{
	dword data[9];
};

struct s_game_results_storage
{
	union
	{
		byte data[0xdcd8];
		struct
		{
			byte valid;
			byte final;
			byte unknown02;
			byte unknown03[0x12b - 3];
			byte has_teams;
			byte unknown12c[4];
			long unknown130;
			long unknown134;
			byte unknown138[0x17c - 0x138];
			dword game_type;
			dword unknown180;
			byte unknown184[0x350 - 0x184];
			s_input_entry_state players[4];
			byte unknown5e0[0xdc4 - 0x5e0];
			s_input_entry teams[16];
			s_input_counters counters;
			s_196d20_sample samples[1000];
			s_input_address addresses[16];
			byte unknowndcd4[4];
		} layout;
	};
};

extern s_game_results_storage g_game_results_storage;

#define g_510cb0 (g_game_results_storage.layout.valid)
#define g_510cb1 (g_game_results_storage.layout.final)
#define g_510cb2 (g_game_results_storage.layout.unknown02)
#define g_510ddb (g_game_results_storage.layout.has_teams)
#define g_510de0 (g_game_results_storage.layout.unknown130)
#define g_510de4 (g_game_results_storage.layout.unknown134)
#define g_510e2c (g_game_results_storage.layout.game_type)
#define g_510e30 (g_game_results_storage.layout.unknown180)
#define g_511000 (g_game_results_storage.layout.players)
#define g_511a74 (g_game_results_storage.layout.teams)
#define g_511bf4 (g_game_results_storage.layout.counters)
#define g_515c34 (g_game_results_storage.layout.samples)
#define g_51e8d4 (g_game_results_storage.layout.addresses)

typedef char assert_game_results_size[(sizeof(s_game_results_storage) == 0xdcd8) ? 1 : -1];
typedef char assert_game_results_has_teams[(offsetof(s_game_results_storage, layout.has_teams) == 0x12b) ? 1 : -1];
typedef char assert_game_results_unknown130[(offsetof(s_game_results_storage, layout.unknown130) == 0x130) ? 1 : -1];
typedef char assert_game_results_game_type[(offsetof(s_game_results_storage, layout.game_type) == 0x17c) ? 1 : -1];
typedef char assert_game_results_players[(offsetof(s_game_results_storage, layout.players) == 0x350) ? 1 : -1];
typedef char assert_game_results_teams[(offsetof(s_game_results_storage, layout.teams) == 0xdc4) ? 1 : -1];
typedef char assert_game_results_counters[(offsetof(s_game_results_storage, layout.counters) == 0xf44) ? 1 : -1];
typedef char assert_game_results_samples[(offsetof(s_game_results_storage, layout.samples) == 0x4f84) ? 1 : -1];
typedef char assert_game_results_addresses[(offsetof(s_game_results_storage, layout.addresses) == 0xdc24) ? 1 : -1];

#endif
