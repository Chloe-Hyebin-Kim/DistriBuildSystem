#pragma once

#include "CoreMinimal.h"
#include "Dom/JsonObject.h"

class FSocket;

/**
 * Wire format shared with the Python agent (Tools/DistriBuild/common.py):
 *   "DBLD" | u32 version | u32 header_len | header JSON (UTF-8) | blob0 | blob1 | ...
 * header["files"] = [{ "name", "size", "csize", "codec": "zlib" | "raw" }, ...]
 */
namespace DistriBuildProtocol
{
	static constexpr uint32 Version = 1;
	static constexpr int32 DefaultPort = 31890;

	struct FBlob
	{
		FString Name;
		TArray<uint8> Data;
	};

	/** Blocking TCP connect with timeout. Returns nullptr on failure. Caller owns the socket (use Close). */
	FSocket* Connect(const FString& Host, int32 Port, float TimeoutSeconds);
	void Close(FSocket*& Socket);

	bool SendFrame(FSocket* Socket, const TSharedRef<FJsonObject>& Header, const TArray<FBlob>& Blobs, int64* OutBytesOnWire = nullptr);
	bool RecvFrame(FSocket* Socket, float TimeoutSeconds, TSharedPtr<FJsonObject>& OutHeader, TArray<FBlob>& OutBlobs, int64* OutBytesOnWire = nullptr);

	/** Splits "host:port" (port optional). */
	bool ParseAddress(const FString& Text, FString& OutHost, int32& OutPort);
}
