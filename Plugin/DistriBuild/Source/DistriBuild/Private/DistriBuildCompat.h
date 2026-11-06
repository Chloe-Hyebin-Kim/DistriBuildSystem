#pragma once

// Engine-version shims for IDistributedBuildController / FTaskCommandData, written against the real
// 4.26 and 5.8 headers. The two differ in the following ways:
//
//                         4.26                            5.8
//   worker arguments      CommandArgs, with -xge_int      ExtraCommandArgs, without -xge_int
//   output file           only in CommandArgs             OutputFileName
//   working directory     folder of InputFileName         WorkingDirectory
//   InitializeController  does not exist, never called    pure virtual, called
//   shader sources        read from disk via the job's    embedded in the job file
//                         absolute directory mappings
//
// Both command lines start with the same positional arguments: "Dir/" ParentPID 0 "In" "Out".
// Fields are detected at compile time from the engine's own header. Written in C++14 for 4.26.

#include "CoreMinimal.h"
#include "DistributedBuildControllerInterface.h"
#include "Misc/Paths.h"
#include "Runtime/Launch/Resources/Version.h"
#include <type_traits>
#include <utility>

#if ENGINE_MAJOR_VERSION >= 5
	#define DISTRIBUILD_OVERRIDE_UE5 override
#else
	#define DISTRIBUILD_OVERRIDE_UE5
#endif

namespace DistriBuildCompat
{
	template <typename T> struct TMemberReturn;
	template <typename C, typename R> struct TMemberReturn<R (C::*)()> { using Type = R; };
	template <typename C, typename R> struct TMemberReturn<R (C::*)() const> { using Type = R; };

	/** Exact return types (including a top-level const) as declared by this engine's interface. */
	using FIsSupportedReturn = typename TMemberReturn<decltype(&IDistributedBuildController::IsSupported)>::Type;
	using FGetNameReturn = typename TMemberReturn<decltype(&IDistributedBuildController::GetName)>::Type;

	template <typename...> struct TVoid { using Type = void; };

#define DISTRIBUILD_DETECT_MEMBER(Member) \
	template <typename T, typename = void> struct THas##Member : std::false_type {}; \
	template <typename T> struct THas##Member<T, typename TVoid<decltype(std::declval<T&>().Member)>::Type> : std::true_type {};

	DISTRIBUILD_DETECT_MEMBER(CommandArgs)
	DISTRIBUILD_DETECT_MEMBER(ExtraCommandArgs)
	DISTRIBUILD_DETECT_MEMBER(WorkingDirectory)
	DISTRIBUILD_DETECT_MEMBER(OutputFileName)

#undef DISTRIBUILD_DETECT_MEMBER

	namespace Private
	{
		template <typename T> FString CommandArgs(const T& D, std::true_type) { return D.CommandArgs; }
		template <typename T> FString CommandArgs(const T&, std::false_type) { return FString(); }

		template <typename T> FString ExtraArgs(const T& D, std::true_type) { return D.ExtraCommandArgs; }
		template <typename T> FString ExtraArgs(const T&, std::false_type) { return FString(); }

		template <typename T> FString WorkingDir(const T& D, std::true_type) { return D.WorkingDirectory; }
		template <typename T> FString WorkingDir(const T& D, std::false_type) { return FPaths::GetPath(D.InputFileName); }

		template <typename T> FString OutputFile(const T& D, std::true_type) { return D.OutputFileName; }
		template <typename T> FString OutputFile(const T&, std::false_type) { return FString(); }

		/** Index just past the next whitespace-separated, quote-aware token. */
		inline int32 SkipToken(const FString& Text, int32 Index)
		{
			const int32 Length = Text.Len();
			while (Index < Length && FChar::IsWhitespace(Text[Index]))
			{
				++Index;
			}
			bool bQuoted = false;
			while (Index < Length && (bQuoted || !FChar::IsWhitespace(Text[Index])))
			{
				if (Text[Index] == TEXT('"'))
				{
					bQuoted = !bQuoted;
				}
				++Index;
			}
			return Index;
		}

		inline int32 SkipTokens(const FString& Text, int32 Count)
		{
			int32 Index = 0;
			for (int32 Token = 0; Token < Count; ++Token)
			{
				Index = SkipToken(Text, Index);
			}
			return Index;
		}

		inline FString WorkerArguments(const FTaskCommandData& D)
		{
			const FString Full = CommandArgs(D, THasCommandArgs<FTaskCommandData>());
			return Full.IsEmpty() ? ExtraArgs(D, THasExtraCommandArgs<FTaskCommandData>()) : Full;
		}
	}

	/** See the table at the top of this file. */
	static constexpr bool bJobsReadShaderSourcesFromDisk = THasCommandArgs<FTaskCommandData>::value;

	inline FString GetWorkingDirectory(const FTaskCommandData& D)
	{
		return Private::WorkingDir(D, THasWorkingDirectory<FTaskCommandData>());
	}

	/** Output file as the engine named it: absolute on 5.8, relative to the working directory on 4.26. */
	inline FString GetOutputFile(const FTaskCommandData& D)
	{
		const FString Direct = Private::OutputFile(D, THasOutputFileName<FTaskCommandData>());
		if (!Direct.IsEmpty())
		{
			return Direct;
		}
		const FString Arguments = Private::WorkerArguments(D);
		const int32 Start = Private::SkipTokens(Arguments, 4);
		return Arguments.Mid(Start, Private::SkipToken(Arguments, Start) - Start).TrimStartAndEnd().TrimQuotes();
	}

	/** ShaderCompileWorker arguments after the positional ones and -xge_int; the agent supplies those itself. */
	inline FString GetWorkerExtraArgs(const FTaskCommandData& D)
	{
		const FString Arguments = Private::WorkerArguments(D);
		FString Extra = Arguments.Mid(Private::SkipTokens(Arguments, 5)).TrimStartAndEnd();
		if (Extra.StartsWith(TEXT("-xge_int")))
		{
			Extra = Extra.Mid(8).TrimStart();
		}
		return Extra;
	}

	/**
	 * Full command line for running the task on this machine. -xge_int must be the sixth argument:
	 * without it ShaderCompileWorker waits for further jobs after the first instead of exiting.
	 */
	inline FString GetLocalCommandLine(const FTaskCommandData& D)
	{
		const FString Arguments = Private::WorkerArguments(D);
		return FString::Printf(TEXT("%s -xge_int %s"), *Arguments.Left(Private::SkipTokens(Arguments, 5)), *GetWorkerExtraArgs(D));
	}
}
