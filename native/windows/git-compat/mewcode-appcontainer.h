#ifndef MEWCODE_APPCONTAINER_H
#define MEWCODE_APPCONTAINER_H

#if defined(MEWCODE_APPCONTAINER_COMPAT) && defined(_WIN32)

#include <windows.h>
#include <io.h>
#include <limits.h>
#include <stdint.h>
#include <string.h>

#define MEWCODE_AC_MAX_PATH 32768
#define MEWCODE_AC_ARRAY_SIZE(a) (sizeof(a) / sizeof((a)[0]))
/* TOKEN_INFORMATION_CLASS value for TokenIsAppContainer. */
#define MEWCODE_TOKEN_IS_APPCONTAINER ((TOKEN_INFORMATION_CLASS)29)

/* Test harnesses may replace APIs, but not the validation logic below. */
#ifdef MEWCODE_APPCONTAINER_TEST
#ifndef MEWCODE_AC_GET_FINAL_PATH
#define MEWCODE_AC_GET_FINAL_PATH GetFinalPathNameByHandleW
#endif
#ifndef MEWCODE_AC_QUERY_DOS_DEVICE
#define MEWCODE_AC_QUERY_DOS_DEVICE QueryDosDeviceW
#endif
#ifndef MEWCODE_AC_GET_CWD
#define MEWCODE_AC_GET_CWD GetCurrentDirectoryW
#endif
#ifndef MEWCODE_AC_CREATE_FILE
#define MEWCODE_AC_CREATE_FILE CreateFileW
#endif
#ifndef MEWCODE_AC_GET_FILE_INFO
#define MEWCODE_AC_GET_FILE_INFO GetFileInformationByHandle
#endif
#ifndef MEWCODE_AC_OPEN_PROCESS_TOKEN
#define MEWCODE_AC_OPEN_PROCESS_TOKEN OpenProcessToken
#endif
#ifndef MEWCODE_AC_GET_TOKEN_INFO
#define MEWCODE_AC_GET_TOKEN_INFO GetTokenInformation
#endif
#ifndef MEWCODE_AC_CLOSE_HANDLE
#define MEWCODE_AC_CLOSE_HANDLE CloseHandle
#endif
#ifndef MEWCODE_AC_GET_HANDLE_INFO
#define MEWCODE_AC_GET_HANDLE_INFO GetHandleInformation
#endif
#ifndef MEWCODE_AC_GET_OSF_HANDLE
#define MEWCODE_AC_GET_OSF_HANDLE _get_osfhandle
#endif
#ifndef MEWCODE_AC_GET_LAST_ERROR
#define MEWCODE_AC_GET_LAST_ERROR GetLastError
#endif
#ifndef MEWCODE_AC_SET_LAST_ERROR
#define MEWCODE_AC_SET_LAST_ERROR SetLastError
#endif
#else
#define MEWCODE_AC_GET_FINAL_PATH GetFinalPathNameByHandleW
#define MEWCODE_AC_QUERY_DOS_DEVICE QueryDosDeviceW
#define MEWCODE_AC_GET_CWD GetCurrentDirectoryW
#define MEWCODE_AC_CREATE_FILE CreateFileW
#define MEWCODE_AC_GET_FILE_INFO GetFileInformationByHandle
#define MEWCODE_AC_OPEN_PROCESS_TOKEN OpenProcessToken
#define MEWCODE_AC_GET_TOKEN_INFO GetTokenInformation
#define MEWCODE_AC_CLOSE_HANDLE CloseHandle
#define MEWCODE_AC_GET_HANDLE_INFO GetHandleInformation
#define MEWCODE_AC_GET_OSF_HANDLE _get_osfhandle
#define MEWCODE_AC_GET_LAST_ERROR GetLastError
#define MEWCODE_AC_SET_LAST_ERROR SetLastError
#endif

static BOOL mewcode_ac_is_appcontainer(void)
{
	HANDLE token = NULL;
	DWORD size = 0;
	BOOL is_appcontainer = FALSE;
	BOOL result = FALSE;

	if (!MEWCODE_AC_OPEN_PROCESS_TOKEN(GetCurrentProcess(), TOKEN_QUERY,
					   &token))
		return FALSE;
	if (MEWCODE_AC_GET_TOKEN_INFO(token,
				      MEWCODE_TOKEN_IS_APPCONTAINER,
				      &is_appcontainer, sizeof(is_appcontainer),
				      &size) &&
	    size == sizeof(is_appcontainer) && is_appcontainer)
		result = TRUE;
	MEWCODE_AC_CLOSE_HANDLE(token);
	return result;
}

static BOOL mewcode_ac_stdio_handles_are_valid(void)
{
	int fd;

	for (fd = 0; fd < 3; fd++) {
		intptr_t raw_handle = MEWCODE_AC_GET_OSF_HANDLE(fd);
		HANDLE handle;
		DWORD flags;

		if (raw_handle == -1 || raw_handle == -2)
			return FALSE;
		handle = (HANDLE)raw_handle;
		if (!handle || handle == INVALID_HANDLE_VALUE ||
		    !MEWCODE_AC_GET_HANDLE_INFO(handle, &flags))
			return FALSE;
	}
	return TRUE;
}

static BOOL mewcode_ac_query_path(HANDLE handle, DWORD flags,
				  WCHAR **path, DWORD *path_len)
{
	DWORD capacity, result;
	WCHAR *buffer;

	*path = NULL;
	*path_len = 0;
	result = MEWCODE_AC_GET_FINAL_PATH(handle, NULL, 0, flags);
	if (!result || result > MEWCODE_AC_MAX_PATH)
		return FALSE;
	capacity = result;

	for (;;) {
		DWORD error;

		if (capacity > MEWCODE_AC_MAX_PATH)
			return FALSE;
		buffer = HeapAlloc(GetProcessHeap(), 0,
				   (size_t)capacity * sizeof(*buffer));
		if (!buffer)
			return FALSE;
		result = MEWCODE_AC_GET_FINAL_PATH(handle, buffer, capacity,
						   flags);
		if (result && result < capacity && buffer[result] == L'\0') {
			*path = buffer;
			*path_len = result;
			return TRUE;
		}
		error = MEWCODE_AC_GET_LAST_ERROR();
		HeapFree(GetProcessHeap(), 0, buffer);
		if (!result) {
			MEWCODE_AC_SET_LAST_ERROR(error);
			return FALSE;
		}
		if (result < capacity)
			return FALSE;
		if (result == capacity) {
			if (capacity == MEWCODE_AC_MAX_PATH)
				return FALSE;
			capacity++;
		} else {
			capacity = result;
		}
	}
}

static BOOL mewcode_ac_equal_path_part(const WCHAR *left, DWORD left_len,
				       const WCHAR *right, DWORD right_len)
{
	if (left_len != right_len || left_len > INT_MAX)
		return FALSE;
	return CompareStringOrdinal(left, (int)left_len, right,
				    (int)right_len, TRUE) == CSTR_EQUAL;
}

static BOOL mewcode_ac_get_volume_parts(HANDLE handle,
					WCHAR **device, DWORD *device_len,
					WCHAR **relative, DWORD *relative_len)
{
	static const WCHAR device_prefix[] = L"\\Device\\";
	WCHAR *nt_path = NULL, *none_path = NULL;
	DWORD nt_len = 0, none_len = 0;
	DWORD prefix_len = (DWORD)(sizeof(device_prefix) /
				   sizeof(device_prefix[0]) - 1);
	BOOL result = FALSE;

	*device = NULL;
	*relative = NULL;
	*device_len = 0;
	*relative_len = 0;
	if (!mewcode_ac_query_path(handle, VOLUME_NAME_NT,
				   &nt_path, &nt_len) ||
	    !mewcode_ac_query_path(handle, VOLUME_NAME_NONE,
				   &none_path, &none_len))
		goto out;
	if (!none_len || none_path[0] != L'\\' || nt_len <= none_len ||
	    !mewcode_ac_equal_path_part(nt_path + nt_len - none_len,
					none_len, none_path, none_len))
		goto out;
	*device_len = nt_len - none_len;
	if (*device_len <= prefix_len || nt_path[*device_len] != L'\\' ||
	    !mewcode_ac_equal_path_part(nt_path, prefix_len,
					device_prefix, prefix_len))
		goto out;
	*device = HeapAlloc(GetProcessHeap(), 0,
			    ((size_t)*device_len + 1) * sizeof(**device));
	if (!*device)
		goto out;
	memcpy(*device, nt_path, (size_t)*device_len * sizeof(**device));
	(*device)[*device_len] = L'\0';
	*relative = none_path;
	*relative_len = none_len;
	none_path = NULL;
	result = TRUE;

out:
	if (nt_path)
		HeapFree(GetProcessHeap(), 0, nt_path);
	if (none_path)
		HeapFree(GetProcessHeap(), 0, none_path);
	if (!result) {
		if (*device)
			HeapFree(GetProcessHeap(), 0, *device);
		*device = NULL;
		*device_len = 0;
	}
	return result;
}

static WCHAR mewcode_ac_dos_drive_for_device(const WCHAR *device,
					     DWORD device_len)
{
	WCHAR letter;
	WCHAR name[3] = L"C:";
	WCHAR target[512];

	for (letter = L'A'; letter <= L'Z'; letter++) {
		DWORD length, first = 0;

		name[0] = letter;
		length = MEWCODE_AC_QUERY_DOS_DEVICE(name, target,
						 MEWCODE_AC_ARRAY_SIZE(target));
		if (!length || length >= MEWCODE_AC_ARRAY_SIZE(target))
			continue;
		/* Compare the first target of the multi-string only. */
		while (first < length && target[first])
			first++;
		if (mewcode_ac_equal_path_part(target, first,
					      device, device_len))
			return letter;
	}
	return L'\0';
}

static BOOL mewcode_ac_same_file(HANDLE left, HANDLE right)
{
	BY_HANDLE_FILE_INFORMATION left_info, right_info;

	if (!MEWCODE_AC_GET_FILE_INFO(left, &left_info) ||
	    !MEWCODE_AC_GET_FILE_INFO(right, &right_info))
		return FALSE;
	return left_info.dwVolumeSerialNumber ==
			 right_info.dwVolumeSerialNumber &&
	       left_info.nFileIndexHigh == right_info.nFileIndexHigh &&
	       left_info.nFileIndexLow == right_info.nFileIndexLow;
}

static BOOL mewcode_ac_make_dos_path(HANDLE handle,
				     WCHAR **dos_path, DWORD *dos_path_len)
{
	WCHAR drive = L'\0';
	WCHAR *target_device = NULL;
	WCHAR *target_relative = NULL, *candidate = NULL;
	DWORD target_device_len = 0;
	DWORD target_relative_len = 0, candidate_len;
	HANDLE reopened = INVALID_HANDLE_VALUE;
	BOOL result = FALSE;

	*dos_path = NULL;
	*dos_path_len = 0;
	if (!mewcode_ac_get_volume_parts(handle, &target_device,
					 &target_device_len, &target_relative,
					 &target_relative_len))
		goto out;
	/* Any DOS-letter volume is acceptable; the reopened candidate below is
	 * verified against the original handle by volume serial and file index,
	 * so a drive alias cannot return a different object. */
	drive = mewcode_ac_dos_drive_for_device(target_device,
					       target_device_len);
	if (drive == L'\0') {
		WCHAR cwd[MEWCODE_AC_MAX_PATH];
		DWORD length = MEWCODE_AC_GET_CWD(
			MEWCODE_AC_ARRAY_SIZE(cwd), cwd);
		/* LPAC may deny the DOS-device namespace. Cwd is only a hint,
		 * never an identity proof. The reopened handle below must
		 * still match the original object. */
		if (length >= 3 && length < MEWCODE_AC_ARRAY_SIZE(cwd) &&
		    cwd[1] == L':' && cwd[2] == L'\\' &&
		    ((cwd[0] >= L'A' && cwd[0] <= L'Z') ||
		     (cwd[0] >= L'a' && cwd[0] <= L'z')))
			drive = cwd[0];
	}
	if (drive == L'\0' || target_relative_len > MEWCODE_AC_MAX_PATH - 7)
		goto out;
	candidate_len = target_relative_len + 6;
	candidate = HeapAlloc(GetProcessHeap(), 0,
			      ((size_t)candidate_len + 1) * sizeof(*candidate));
	if (!candidate)
		goto out;
	memcpy(candidate, L"\\\\?\\", 4 * sizeof(*candidate));
	candidate[4] = drive;
	candidate[5] = L':';
	memcpy(candidate + 6, target_relative,
	       ((size_t)target_relative_len + 1) * sizeof(*candidate));

	reopened = MEWCODE_AC_CREATE_FILE(candidate, 0,
					  FILE_SHARE_READ | FILE_SHARE_WRITE |
					  FILE_SHARE_DELETE, NULL,
					  OPEN_EXISTING,
					  FILE_FLAG_BACKUP_SEMANTICS, NULL);
	if (reopened == INVALID_HANDLE_VALUE ||
	    !mewcode_ac_same_file(handle, reopened))
		goto out;
	*dos_path = candidate;
	*dos_path_len = candidate_len;
	candidate = NULL;
	result = TRUE;

out:
	if (reopened != INVALID_HANDLE_VALUE)
		MEWCODE_AC_CLOSE_HANDLE(reopened);
	if (candidate)
		HeapFree(GetProcessHeap(), 0, candidate);
	if (target_device)
		HeapFree(GetProcessHeap(), 0, target_device);
	if (target_relative)
		HeapFree(GetProcessHeap(), 0, target_relative);
	return result;
}

static DWORD mewcode_appcontainer_get_final_path_name_by_handle_w(
	HANDLE handle, LPWSTR path, DWORD size, DWORD flags)
{
	DWORD result, original_error, needed;
	WCHAR *dos_path = NULL;
	DWORD dos_path_len = 0;

	result = MEWCODE_AC_GET_FINAL_PATH(handle, path, size, flags);
	if (result || flags != 0)
		return result;
	original_error = MEWCODE_AC_GET_LAST_ERROR();
	if (original_error != ERROR_ACCESS_DENIED || (!path && size) ||
	    !mewcode_ac_is_appcontainer() ||
	    !mewcode_ac_make_dos_path(handle, &dos_path, &dos_path_len))
		goto failed;
	if (dos_path_len == MAXDWORD)
		goto failed;
	needed = dos_path_len + 1;
	if (size < needed) {
		HeapFree(GetProcessHeap(), 0, dos_path);
		MEWCODE_AC_SET_LAST_ERROR(ERROR_INSUFFICIENT_BUFFER);
		return needed;
	}
	memcpy(path, dos_path, (size_t)needed * sizeof(*path));
	HeapFree(GetProcessHeap(), 0, dos_path);
	MEWCODE_AC_SET_LAST_ERROR(ERROR_SUCCESS);
	return dos_path_len;

failed:
	if (dos_path)
		HeapFree(GetProcessHeap(), 0, dos_path);
	MEWCODE_AC_SET_LAST_ERROR(original_error);
	return 0;
}

#endif /* MEWCODE_APPCONTAINER_COMPAT && _WIN32 */

#endif /* MEWCODE_APPCONTAINER_H */
