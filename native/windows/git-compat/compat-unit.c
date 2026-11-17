/* Host-only deterministic API tests. Never linked into the Git candidate. */
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <io.h>
#include <stdint.h>
#include <stdio.h>
#include <wchar.h>

static int mode, api_calls, reopen_calls;
static DWORD mock_error;
enum {
	NORMAL, NOT_CONTAINER, TOKEN_FAILURE, INVALID_FD, INVALID_HANDLE,
	CROSS_VOLUME, UNMAPPED_VOLUME, BAD_SUFFIX, REOPEN_FAILURE,
	DIFFERENT_FILE, INFO_FAILURE, WRONG_ERROR, API_SUCCESS,
	OVERFLOW_PATH, SMALL_BUFFER, HANDLE_FAILURE, DOS_DENIED_C, DOS_DENIED_E
};

static DWORD mock_final(HANDLE h, LPWSTR out, DWORD size, DWORD flags)
{
	const WCHAR *value;
	DWORD count;
	(void)h;
	api_calls++;
	if (!flags) {
		if (mode != API_SUCCESS) {
			mock_error = mode == WRONG_ERROR ? ERROR_INVALID_HANDLE :
				ERROR_ACCESS_DENIED;
			return 0;
		}
		value = L"native-result";
	} else if (flags == VOLUME_NAME_NT) {
		if (mode == OVERFLOW_PATH)
			return 32769;
		if (mode == CROSS_VOLUME || mode == DOS_DENIED_E)
			value = L"\\Device\\HarddiskVolume4\\fixture\\item";
		else if (mode == UNMAPPED_VOLUME)
			value = L"\\Device\\HarddiskVolume9\\fixture\\item";
		else
			value = L"\\Device\\HarddiskVolume3\\fixture\\item";
	} else if (flags == VOLUME_NAME_NONE) {
		value = mode == BAD_SUFFIX ? L"\\wrong\\item" : L"\\fixture\\item";
	} else {
		mock_error = ERROR_INVALID_PARAMETER;
		return 0;
	}
	count = (DWORD)wcslen(value);
	if (size <= count)
		return count + 1;
	memcpy(out, value, (count + 1) * sizeof(WCHAR));
	return count;
}

static DWORD mock_query_dos(LPCWSTR name, LPWSTR out, DWORD size)
{
	/* Multi-string results: the device target plus two terminators. */
	static const WCHAR volume3[] = L"\\Device\\HarddiskVolume3";
	static const WCHAR volume4[] = L"\\Device\\HarddiskVolume4";
	const WCHAR *value = NULL;
	DWORD count;
	if (mode == DOS_DENIED_C || mode == DOS_DENIED_E)
		return 0;

	if (!wcscmp(name, L"C:"))
		value = volume3;
	else if (!wcscmp(name, L"E:"))
		value = volume4;
	if (!value)
		return 0;
	count = (DWORD)wcslen(value);
	if (size <= count + 1)
		return 0;
	memcpy(out, value, count * sizeof(WCHAR));
	out[count] = L'\0';
	out[count + 1] = L'\0';
	return count + 2;
}

static DWORD mock_cwd(DWORD size, LPWSTR out)
{
	const WCHAR *value = mode == DOS_DENIED_E ? L"E:\\fixture" : L"C:\\fixture";
	DWORD count = (DWORD)wcslen(value);
	if (size <= count) return count + 1;
	memcpy(out, value, (count + 1) * sizeof(WCHAR));
	return count;
}

static HANDLE mock_create(LPCWSTR name, DWORD access, DWORD share,
	LPSECURITY_ATTRIBUTES attributes, DWORD disposition, DWORD flags,
	HANDLE template_file)
{
	if (access || share != 7 || attributes || disposition != OPEN_EXISTING ||
	    flags != FILE_FLAG_BACKUP_SEMANTICS || template_file)
		return INVALID_HANDLE_VALUE;
	reopen_calls++;
	if (mode == REOPEN_FAILURE)
		return INVALID_HANDLE_VALUE;
	if (wcscmp(name, L"\\\\?\\C:\\fixture\\item") &&
	    wcscmp(name, L"\\\\?\\E:\\fixture\\item"))
		return INVALID_HANDLE_VALUE;
	return (HANDLE)(uintptr_t)3;
}

static BOOL mock_info(HANDLE h, LPBY_HANDLE_FILE_INFORMATION value)
{
	if (mode == INFO_FAILURE)
		return FALSE;
	ZeroMemory(value, sizeof(*value));
	value->dwVolumeSerialNumber = mode == UNMAPPED_VOLUME && h == (HANDLE)(uintptr_t)3 ? 99 : 42;
	value->nFileIndexHigh = 7;
	value->nFileIndexLow = mode == DIFFERENT_FILE &&
		h == (HANDLE)(uintptr_t)3 ? 99 : 21;
	return TRUE;
}

static BOOL mock_open_token(HANDLE process, DWORD access, PHANDLE token)
{
	(void)process;
	if (mode == TOKEN_FAILURE || access != TOKEN_QUERY)
		return FALSE;
	*token = (HANDLE)(uintptr_t)1;
	return TRUE;
}

static BOOL mock_token_info(HANDLE token, TOKEN_INFORMATION_CLASS type,
	LPVOID value, DWORD size, PDWORD needed)
{
	(void)token;
	if ((int)type != 29 || size != sizeof(BOOL))
		return FALSE;
	*(BOOL *)value = mode != NOT_CONTAINER;
	*needed = sizeof(BOOL);
	return TRUE;
}

static BOOL mock_close(HANDLE h) { (void)h; return TRUE; }
static intptr_t mock_fd(int fd)
{
	if (fd == 1 && mode == INVALID_FD)
		return -2;
	if (fd == 2 && mode == INVALID_HANDLE)
		return -1;
	return (intptr_t)(fd + 10);
}
static BOOL mock_handle_info(HANDLE h, LPDWORD flags)
{
	(void)h;
	*flags = 0;
	return mode != HANDLE_FAILURE;
}
static DWORD mock_last_error(void) { return mock_error; }
static void mock_set_error(DWORD value) { mock_error = value; }

#define MEWCODE_APPCONTAINER_COMPAT
#define MEWCODE_APPCONTAINER_TEST
#define MEWCODE_AC_GET_FINAL_PATH mock_final
#define MEWCODE_AC_QUERY_DOS_DEVICE mock_query_dos
#define MEWCODE_AC_GET_CWD mock_cwd
#define MEWCODE_AC_CREATE_FILE mock_create
#define MEWCODE_AC_GET_FILE_INFO mock_info
#define MEWCODE_AC_OPEN_PROCESS_TOKEN mock_open_token
#define MEWCODE_AC_GET_TOKEN_INFO mock_token_info
#define MEWCODE_AC_CLOSE_HANDLE mock_close
#define MEWCODE_AC_GET_HANDLE_INFO mock_handle_info
#define MEWCODE_AC_GET_OSF_HANDLE mock_fd
#define MEWCODE_AC_GET_LAST_ERROR mock_last_error
#define MEWCODE_AC_SET_LAST_ERROR mock_set_error
#include "mewcode-appcontainer.h"

#define CHECK(condition) do { if (!(condition)) { \
	fprintf(stderr, "FAIL line=%d mode=%d\n", __LINE__, mode); \
	return 1; } } while (0)

int main(void)
{
	WCHAR out[256];
	DWORD result, needed;
	int tests = 0;
	int failures[] = {NOT_CONTAINER, TOKEN_FAILURE, UNMAPPED_VOLUME,
		BAD_SUFFIX, REOPEN_FAILURE, DIFFERENT_FILE,
		INFO_FAILURE, WRONG_ERROR, OVERFLOW_PATH};
	size_t i;
	mode = NORMAL;
	CHECK(mewcode_ac_stdio_handles_are_valid());
	tests++;
	for (mode = INVALID_FD; mode <= INVALID_HANDLE; mode++) {
		CHECK(!mewcode_ac_stdio_handles_are_valid());
		tests++;
	}
	mode = HANDLE_FAILURE;
	CHECK(!mewcode_ac_stdio_handles_are_valid());
	tests++;
	mode = NORMAL;
	result = mewcode_appcontainer_get_final_path_name_by_handle_w(
		(HANDLE)(uintptr_t)4, out, 256, 0);
	CHECK(result == wcslen(L"\\\\?\\C:\\fixture\\item"));
	CHECK(!wcscmp(out, L"\\\\?\\C:\\fixture\\item"));
	CHECK(reopen_calls == 1);
	tests++;
	for (mode = DOS_DENIED_C; mode <= DOS_DENIED_E; mode++) {
		result = mewcode_appcontainer_get_final_path_name_by_handle_w(
			(HANDLE)(uintptr_t)4, out, 256, 0);
		CHECK(result == wcslen(L"\\\\?\\C:\\fixture\\item"));
		CHECK(out[4] == (mode == DOS_DENIED_E ? L'E' : L'C'));
		tests++;
	}
	mode = CROSS_VOLUME;
	reopen_calls = 0;
	result = mewcode_appcontainer_get_final_path_name_by_handle_w(
		(HANDLE)(uintptr_t)4, out, 256, 0);
	CHECK(result == wcslen(L"\\\\?\\E:\\fixture\\item"));
	CHECK(!wcscmp(out, L"\\\\?\\E:\\fixture\\item"));
	CHECK(reopen_calls == 1);
	tests++;
	mode = NORMAL;
	needed = result + 1;
	out[0] = L'!';
	result = mewcode_appcontainer_get_final_path_name_by_handle_w(
		(HANDLE)(uintptr_t)4, out, 1, 0);
	CHECK(result == needed && out[0] == L'!');
	CHECK(mock_error == ERROR_INSUFFICIENT_BUFFER);
	tests++;
	CHECK(mewcode_appcontainer_get_final_path_name_by_handle_w(
		(HANDLE)(uintptr_t)4, NULL, 0, 0) == needed);
	tests++;
	CHECK(!mewcode_appcontainer_get_final_path_name_by_handle_w(
		(HANDLE)(uintptr_t)4, NULL, 1, 0));
	tests++;
	for (i = 0; i < sizeof(failures) / sizeof(failures[0]); i++) {
		mode = failures[i];
		out[0] = L'!';
		CHECK(!mewcode_appcontainer_get_final_path_name_by_handle_w(
			(HANDLE)(uintptr_t)4, out, 256, 0));
		CHECK(out[0] == L'!');
		CHECK(mock_error == (mode == WRONG_ERROR ?
			ERROR_INVALID_HANDLE : ERROR_ACCESS_DENIED));
		tests++;
	}
	mode = API_SUCCESS;
	api_calls = reopen_calls = 0;
	CHECK(mewcode_appcontainer_get_final_path_name_by_handle_w(
		(HANDLE)(uintptr_t)4, out, 256, 0) == 13);
	CHECK(api_calls == 1 && reopen_calls == 0);
	tests++;
	mode = NORMAL;
	api_calls = reopen_calls = 0;
	CHECK(!mewcode_appcontainer_get_final_path_name_by_handle_w(
		(HANDLE)(uintptr_t)4, out, 256, FILE_NAME_OPENED));
	CHECK(api_calls == 1 && reopen_calls == 0);
	tests++;
	printf("MEWCODE_GIT_COMPAT_UNIT_OK cases=%d\n", tests);
	return 0;
}
