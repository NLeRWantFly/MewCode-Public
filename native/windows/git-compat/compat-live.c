/* Standalone functional probe; never linked into the Git candidate. */
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <stdio.h>
#include <wchar.h>
#define MEWCODE_APPCONTAINER_COMPAT
#include "mewcode-appcontainer.h"

int main(void)
{
	WCHAR path[32768], small[1] = {L'!'};
	DWORD original, original_error, count, needed;
	HANDLE h, again;
	BOOL same;
	int fd;
	printf("appcontainer=%d\n", !!mewcode_ac_is_appcontainer());
	if (!mewcode_ac_stdio_handles_are_valid())
		return 2;
	h = CreateFileW(L".", 0, 7, NULL, OPEN_EXISTING,
		FILE_FLAG_BACKUP_SEMANTICS, NULL);
	if (h == INVALID_HANDLE_VALUE)
		return 3;
	original = GetFinalPathNameByHandleW(h, path, 32768, 0);
	original_error = original ? 0 : GetLastError();
	printf("original_dos_ok=%d error=%lu\n", !!original, original_error);
	count = mewcode_appcontainer_get_final_path_name_by_handle_w(
		h, path, 32768, 0);
	if (!count || count >= 32768)
		return 4;
	again = CreateFileW(path, 0, 7, NULL, OPEN_EXISTING,
		FILE_FLAG_BACKUP_SEMANTICS, NULL);
	if (again == INVALID_HANDLE_VALUE)
		return 5;
	same = mewcode_ac_same_file(h, again);
	CloseHandle(again);
	if (!same)
		return 6;
	needed = mewcode_appcontainer_get_final_path_name_by_handle_w(
		h, small, 1, 0);
	CloseHandle(h);
	if (needed != count + 1 || (!original && small[0] != L'!'))
		return 7;
	fd = _dup(0);
	if (fd < 0)
		return 8;
	_close(0);
	if (mewcode_ac_stdio_handles_are_valid())
		return 9;
	if (_dup2(fd, 0))
		return 10;
	_close(fd);
	puts("MEWCODE_GIT_COMPAT_LIVE_OK same_file=1 buffer=1 invalid_fd=1");
	return 0;
}
