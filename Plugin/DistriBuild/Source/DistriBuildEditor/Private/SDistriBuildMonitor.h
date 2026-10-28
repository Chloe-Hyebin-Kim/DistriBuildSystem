#pragma once

#include "CoreMinimal.h"
#include "DistriBuildModule.h"
#include "Widgets/SCompoundWidget.h"
#include "Widgets/Views/SListView.h"

/** Agent list + live counters, shown in Window > Developer Tools > DistriBuild Monitor. */
class SDistriBuildMonitor : public SCompoundWidget
{
public:
	SLATE_BEGIN_ARGS(SDistriBuildMonitor) {}
	SLATE_END_ARGS()

	void Construct(const FArguments& InArgs);

private:
	using FItem = TSharedPtr<FDistriBuildAgentSnapshot>;

	EActiveTimerReturnType Poll(double InCurrentTime, float InDeltaTime);
	TSharedRef<ITableRow> MakeRow(FItem Item, const TSharedRef<STableViewBase>& Owner);
	FText GetSummaryText() const;
	FText GetStatsText() const;

	TArray<FItem> Items;
	TSharedPtr<SListView<FItem>> List;
	FDistriBuildStats Stats;
	int32 TotalSlots = 0;
	int32 UsableSlots = 0;
};
